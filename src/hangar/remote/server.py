import configparser
import os
import shutil
import tempfile
import traceback
import warnings
from concurrent import futures
from os.path import join as pjoin
from pathlib import Path
from pprint import pprint as pp
from threading import Lock
from typing import Union, Iterable

import blosc
import grpc
import lmdb

from . import (
    chunks,
    hangar_service_pb2,
    hangar_service_pb2_grpc,
    request_header_validator_interceptor,
)
from .content import ContentWriter, DataWriter
from .. import constants as c
from ..backends import BACKEND_ACCESSOR_MAP, backend_decoder
from ..context import Environments
from ..records import (
    commiting,
    hashs,
    heads,
    parsing,
    queries,
    summarize,
    hash_schema_db_key_from_raw_key,
    hash_data_db_key_from_raw_key,
)
from ..records.hashmachine import hash_func_from_tcode
from ..txnctx import TxnRegister
from ..utils import set_blosc_nthreads

set_blosc_nthreads()


def server_config(server_dir, *, create: bool = True) -> configparser.ConfigParser:
    CFG = configparser.ConfigParser()
    dst_dir = Path(server_dir)
    dst_path = dst_dir.joinpath(c.CONFIG_SERVER_NAME)
    if dst_path.is_file():
        CFG.read(dst_path)
        print(f'Found Config File at {dst_path}')
    else:
        if create:
            dst_dir.mkdir(exist_ok=True)
            print(f'Creating Server Config File in {dst_path}')
            src_path = Path(os.path.dirname(__file__), c.CONFIG_SERVER_NAME)
            shutil.copyfile(src_path, dst_path)
            CFG.read(src_path)
        else:
            src_path = Path(os.path.dirname(__file__), c.CONFIG_SERVER_NAME)
            CFG.read(src_path)
    return CFG


def context_abort_with_exception_traceback(
        context: grpc.ServicerContext,
        exc: Exception,
        status_code: grpc.StatusCode
):
    context.abort(
        code=status_code,
        details=(f'Exception Type: {type(exc)} \n'
                 f'Exception Message: {exc} \n'
                 f'Traceback: \n {traceback.format_tb(exc.__traceback__)}'))


def context_abort_with_handled_error(
        context: grpc.ServicerContext,
        message: str, status_code:
        grpc.StatusCode
):
    context.abort(code=status_code, details=message)


class HangarServer(hangar_service_pb2_grpc.HangarServiceServicer):

    def __init__(self, repo_path: Union[str, bytes, Path], overwrite=False):

        if isinstance(repo_path, (str, bytes)):
            repo_path = Path(repo_path)

        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            envs = Environments(pth=repo_path)
        self.env: Environments = envs
        self.data_writer_lock = Lock()
        self.hash_reader_lock = Lock()

        try:
            self.env.init_repo(
                user_name='SERVER_USER',
                user_email='SERVER_USER@HANGAR.SERVER',
                remove_old=overwrite)
        except OSError:
            pass

        self._rFs = {}
        for backend, accessor in BACKEND_ACCESSOR_MAP.items():
            if accessor is not None:
                self._rFs[backend] = accessor(
                    repo_path=self.env.repo_path,
                    schema_shape=None,
                    schema_dtype=None)
                self._rFs[backend].open(mode='r')

        self.CFG = server_config(repo_path, create=True)
        print(f'Server Started with Config:')
        pp({k: dict(v) for k, v in self.CFG.items()})
        self.txnregister = TxnRegister()
        self.repo_path = self.env.repo_path
        self.data_dir = pjoin(self.repo_path, c.DIR_DATA)
        self.CW = ContentWriter(self.env)
        self.DW = DataWriter(self.env)

    def close(self):
        for backend_accessor in self._rFs.values():
            backend_accessor.close()
        self.env._close_environments()

    # -------------------- Client Config --------------------------------------

    def PING(self, request, context):
        """Test function. PING -> PONG!
        """
        reply = hangar_service_pb2.PingReply(result='PONG')
        return reply

    def GetClientConfig(self, request, context):
        """Return parameters to the client to set up channel options as desired by the server.
        """
        clientCFG = self.CFG['CLIENT_GRPC']
        push_max_nbytes = clientCFG['push_max_nbytes']
        enable_compression = clientCFG['enable_compression']
        optimization_target = clientCFG['optimization_target']

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.GetClientConfigReply(error=err)
        reply.config['push_max_nbytes'] = push_max_nbytes
        reply.config['enable_compression'] = enable_compression
        reply.config['optimization_target'] = optimization_target
        return reply

    # -------------------- Branch Record --------------------------------------

    def FetchBranchRecord(self, request, context):
        """Return the current HEAD commit of a particular branch
        """
        branch_name = request.rec.name
        try:
            head = heads.get_branch_head_commit(self.env.branchenv, branch_name)
            rec = hangar_service_pb2.BranchRecord(name=branch_name, commit=head)
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
            reply = hangar_service_pb2.FetchBranchRecordReply(rec=rec, error=err)
            return reply
        except ValueError:
            msg = f'BRANCH: {branch_name} DOES NOT EXIST ON SERVER.'
            context_abort_with_handled_error(
                context=context, message=msg, status_code=grpc.StatusCode.NOT_FOUND)
            return

    def PushBranchRecord(self, request, context):
        """Update the HEAD commit of a branch, creating the record if not previously existing.
        """
        branch_name = request.rec.name
        commit = request.rec.commit
        branch_names = heads.get_branch_names(self.env.branchenv)
        if branch_name not in branch_names:
            heads.create_branch(self.env.branchenv, name=branch_name, base_commit=commit)
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        else:
            current_head = heads.get_branch_head_commit(self.env.branchenv, branch_name)
            if current_head == commit:
                msg = f'NO CHANGE TO BRANCH: {branch_name} WITH HEAD: {current_head}'
                context_abort_with_handled_error(
                    context=context, message=msg, status_code=grpc.StatusCode.ALREADY_EXISTS)
                return
            else:
                heads.set_branch_head_commit(self.env.branchenv, branch_name, commit)
                err = hangar_service_pb2.ErrorProto(code=0, message='OK')

        reply = hangar_service_pb2.PushBranchRecordReply(error=err)
        return reply

    # -------------------------- Commit Record --------------------------------

    def FetchCommit(self, request, context):
        """Return raw data representing contents, spec, and parents of a commit hash.
        """
        commit = request.commit
        commitRefKey = parsing.commit_ref_db_key_from_raw_key(commit)
        commitParentKey = parsing.commit_parent_db_key_from_raw_key(commit)
        commitSpecKey = parsing.commit_spec_db_key_from_raw_key(commit)

        reftxn = self.txnregister.begin_reader_txn(self.env.refenv)
        try:
            commitRefVal = reftxn.get(commitRefKey, default=False)
            commitParentVal = reftxn.get(commitParentKey, default=False)
            commitSpecVal = reftxn.get(commitSpecKey, default=False)
        finally:
            self.txnregister.abort_reader_txn(self.env.refenv)

        if commitRefVal is False:
            msg = f'COMMIT: {commit} DOES NOT EXIST ON SERVER'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            err = hangar_service_pb2.ErrorProto(code=5, message=msg)
            reply = hangar_service_pb2.FetchCommitReply(commit=commit, error=err)
            yield reply
            raise StopIteration()
        else:
            raw_data_chunks = chunks.chunk_bytes(commitRefVal)
            bsize = len(commitRefVal)
            commit_proto = hangar_service_pb2.CommitRecord()
            commit_proto.parent = commitParentVal
            commit_proto.spec = commitSpecVal
            reply = hangar_service_pb2.FetchCommitReply(commit=commit, total_byte_size=bsize)
            for chunk in raw_data_chunks:
                commit_proto.ref = chunk
                reply.record.CopyFrom(commit_proto)
                yield reply

    def PushCommit(self, request_iterator, context):
        """Record the contents of a new commit sent to the server.

        Will not overwrite data if a commit hash is already recorded on the server.
        """
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                refBytes, offset = bytearray(request.total_byte_size), 0
                specVal = request.record.spec
                parentVal = request.record.parent
            size = len(request.record.ref)
            refBytes[offset: offset + size] = request.record.ref
            offset += size

        digest = self.CW.commit(commit, parentVal, specVal, refBytes)
        if not digest:
            msg = f'COMMIT: {commit} ALREADY EXISTS'
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(msg)
            err = hangar_service_pb2.ErrorProto(code=6, message=msg)
        else:
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
            commiting.move_process_data_to_store(self.env.repo_path, remote_operation=True)

        reply = hangar_service_pb2.PushCommitReply(error=err)
        return reply

    # --------------------- Schema Record -------------------------------------

    def FetchSchema(self, request, context):
        """Return the raw byte specification of a particular schema with requested hash.
        """
        schema_hash = request.rec.digest
        schemaKey = hash_schema_db_key_from_raw_key(schema_hash)
        hashTxn = self.txnregister.begin_reader_txn(self.env.hashenv)
        try:
            schemaExists = hashTxn.get(schemaKey, default=False)
            if schemaExists is not False:
                print(f'found schema: {schema_hash}')
                rec = hangar_service_pb2.SchemaRecord(digest=schema_hash, blob=schemaExists)
                err = hangar_service_pb2.ErrorProto(code=0, message='OK')
            else:
                print(f'not exists: {schema_hash}')
                msg = f'SCHEMA HASH: {schema_hash} DOES NOT EXIST ON SERVER'
                context.set_details(msg)
                context.set_code(grpc.StatusCode.NOT_FOUND)
                err = hangar_service_pb2.ErrorProto(code=5, message=msg)
                rec = hangar_service_pb2.SchemaRecord(digest=schema_hash)
        finally:
            self.txnregister.abort_reader_txn(self.env.hashenv)

        reply = hangar_service_pb2.FetchSchemaReply(rec=rec, error=err)
        return reply

    def PushSchema(self, request, context):
        """Add a new schema byte specification record.

        Will not overwrite a schema hash which already exists on the server.
        """
        schema_hash = request.rec.digest
        schema_val = request.rec.blob

        digest = self.CW.schema(schema_hash, schema_val)
        if not digest:
            print(f'exists: {schema_val}')
            msg = f'SCHEMA: {schema_hash} ALREADY EXISTS ON SERVER'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            err = hangar_service_pb2.ErrorProto(code=6, message=msg)
        else:
            print(f'created new: {schema_val}')
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.PushSchemaReply(error=err)
        return reply

    # ---------------------------- Data ---------------------------------------

    def FetchFindDataOrigin(self, request_iterator, context):
        digests = []
        for request in request_iterator:
            digests.append(request.digest)

        hashTxn = self.txnregister.begin_reader_txn(self.env.hashenv)
        try:
            for digest in digests:
                hashKey = hash_data_db_key_from_raw_key(digest)
                hashVal = hashTxn.get(hashKey, default=False)
                if hashVal is False:
                    msg = f'HASH DOES NOT EXIST: {hashKey}'
                    context.set_details(msg)
                    context.set_code(grpc.StatusCode.NOT_FOUND)
                    err = hangar_service_pb2.ErrorProto(code=5, message=msg)
                    reply = hangar_service_pb2.FetchDataReply(error=err)
                    yield reply
                    raise StopIteration()
                else:
                    spec = backend_decoder(hashVal)
                    if spec.backend in ['01', '00', '10']:
                        dtype = hangar_service_pb2.DataType.NP_ARRAY
                    elif spec.backend == '30':
                        dtype = hangar_service_pb2.DataType.STR
                    elif spec.backend == '31':
                        dtype = hangar_service_pb2.DataType.BYTES
                    else:
                        raise TypeError(spec)

                    response = hangar_service_pb2.DataOriginReply(
                        location=hangar_service_pb2.DataLocation.REMOTE_SERVER,
                        data_type=dtype,
                        digest=digest,
                        uri=digest,
                        compression=True,
                    )
                    response.compression_opts['id'] = 'blosc'
                    response.compression_opts['cname'] = 'blosclz'
                    response.compression_opts['clevel'] = '3'
                    yield response

        finally:
            self.txnregister.abort_reader_txn(self.env.hashenv)

    def FetchData(self, request, context):
        """Return a packed byte representation of samples corresponding to a digest.

        Please see comments below which explain why not all requests are
        guaranteed to fully complete in one operation.

        We receive a list of digests to send to the client. One consideration
        we have is that there is no way to know how much memory will be used
        when the data is read from disk. Samples are compressed against
        each-other before going over the wire, which means its preferable to
        read in as much as possible. However, since we don't want to overload
        the client system when the binary blob is decompressed into individual
        tensors, we set some maximum size which tensors can occupy when
        uncompressed. When we receive a list of digests whose data size is in
        excess of this limit, we just say sorry to the client, send the chunk
        of digests/tensors off to them as is (incomplete), and request that
        the client figure out what it still needs and ask us again.
        """
        uri = request.uri
        hashKey = hash_data_db_key_from_raw_key(uri)
        try:
            with self.hash_reader_lock:
                hashTxn = self.txnregister.begin_reader_txn(self.env.hashenv)
                hashVal = hashTxn.get(hashKey, default=False)
                self.txnregister.abort_reader_txn(self.env.hashenv)
        except Exception as e:
            context_abort_with_exception_traceback(
                context=context, exc=e, status_code=grpc.StatusCode.INTERNAL)
            raise e

        if hashVal is False:
            exc = FileNotFoundError(f'request uri does not exist. URI: {uri}')
            context_abort_with_exception_traceback(
                context=context, exc=exc, status_code=grpc.StatusCode.NOT_FOUND)

        spec = backend_decoder(hashVal)
        data = self._rFs[spec.backend].read_data(spec)
        dtype_code, raw_record = chunks.serialize_data(data)
        compressed_record = blosc.compress(
            raw_record, clevel=3, cname='blosclz', shuffle=blosc.NOSHUFFLE)

        def replies_iterator(raw, uri, error_proto):
            reply = hangar_service_pb2.FetchDataReply(
                uri=uri,
                nbytes=len(raw),
                error=error_proto)
            for raw_chunk in chunks.chunk_bytes(raw):
                reply.raw_data = raw_chunk
                yield reply

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        repliesIter = replies_iterator(compressed_record, uri, err)
        yield from repliesIter

    def PushFindDataOrigin(
            self,
            request_iterator: Iterable[hangar_service_pb2.PushFindDataOriginRequest],
            context
    ) -> hangar_service_pb2.PushFindDataOriginReply:

        CONFIG_SEND_LOCATION = hangar_service_pb2.DataLocation.REMOTE_SERVER

        all_requests = [req for req in request_iterator]
        for request in all_requests:
            if request.compression_is_desired is True:
                reply_compression_expected = True
                if request.data_type == hangar_service_pb2.DataType.NP_ARRAY:
                    reply_compression_opts_expected = {
                        'id': 'blosc',
                        'cname': 'blosclz',
                        'clevel': '3'
                    }
                elif request.data_type == hangar_service_pb2.DataType.STR:
                    reply_compression_opts_expected = {
                        'id': 'blosc',
                        'cname': 'zstd',
                        'clevel': '3'
                    }
                elif request.data_type == hangar_service_pb2.DataType.BYTES:
                    reply_compression_opts_expected = {
                        'id': 'blosc',
                        'cname': 'blosclz',
                        'clevel': '3'
                    }
                else:
                    raise TypeError(request)
            else:
                reply_compression_expected = False
                reply_compression_opts_expected = {}

            if CONFIG_SEND_LOCATION == hangar_service_pb2.DataLocation.REMOTE_SERVER:
                reply_uri = request.digest
            else:
                raise RuntimeError(f'CONFIG_SEND_LOCATION: {CONFIG_SEND_LOCATION}')

            reply = hangar_service_pb2.PushFindDataOriginReply(
                digest=request.digest,
                location=CONFIG_SEND_LOCATION,
                uri=reply_uri,
                compression_expected=reply_compression_expected,
                compression_opts_expected=reply_compression_opts_expected,
            )
            yield reply

    def PushBeginContext(self, request, context):
        try:
            self.DW.__enter__()
        except Exception as e:
            context.abort(
                code=grpc.StatusCode.INTERNAL,
                details=(f'Exception Type: {type(e)} \n'
                         f'Exception Message: {e} \n'
                         f'Traceback: \n {traceback.format_tb(e.__traceback__)}')
            )
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.PushBeginContextReply(err=err)
        return reply

    def PushEndContext(self, request, context):
        try:
            self.DW.__exit__()
        except Exception as e:
            context.abort(
                code=grpc.StatusCode.INTERNAL,
                details=(f'Exception Type: {type(e)} \n'
                         f'Exception Message: {e} \n'
                         f'Traceback: \n {traceback.format_tb(e.__traceback__)}')
            )
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.PushEndContextReply(err=err)
        return reply

    def PushData(
            self,
            request_iterator: Iterable[hangar_service_pb2.PushDataRequest],
            context: grpc.ServicerContext
    ) -> hangar_service_pb2.PushDataReply:
        """Receive compressed streams of binary data from the client.

        In order to prevent errors or malicious behavior, the cryptographic hash
        of every tensor is calculated and compared to what the client "said" it
        is. If an error is detected, no sample in the entire stream will be
        saved to disk.
        """

        for idx, request in enumerate(request_iterator):
            if idx == 0:
                if not self.DW.is_cm:
                    context.abort(
                        code=grpc.StatusCode.FAILED_PRECONDITION,
                        details=f'Attept to push without opening context'
                    )
                uri = request.uri
                dtype_code = request.data_type
                schema_hash = request.schema_hash
                dBytes = bytearray(request.nbytes)
                offset = 0
            size = len(request.raw_data)
            dBytes[offset: offset + size] = request.raw_data
            offset += size

        # TODO: Handle expected vs required
        uncompBytes = blosc.decompress(dBytes)

        recieved_data = chunks.deserialize_data(dtype_code, uncompBytes)
        hash_func = hash_func_from_tcode(str(dtype_code))
        recieved_hash = hash_func(recieved_data)

        # TODO: uri is not the correct name for this
        if recieved_hash != uri:
            context.abort(
                code=grpc.StatusCode.DATA_LOSS,
                details=f'HASH MANGLED, received: {recieved_hash} != expected digest: {uri}'
            )
        try:
            with self.data_writer_lock:
                _ = self.DW.data(schema_hash, data_digest=recieved_hash, data=recieved_data)  # returns saved)_digests
        except Exception as e:
            context.abort(
                code=grpc.StatusCode.INTERNAL,
                details=(f'Exception Type: {type(e)} \n'
                         f'Exception Message: {e} \n'
                         f'Traceback: \n {traceback.format_tb(e.__traceback__)}')
            )
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.PushDataReply(error=err)
        return reply

    # ------------------------ Fetch Find Missing -----------------------------------

    def FetchFindMissingCommits(self, request, context):
        """Determine commit digests existing on the server which are not present on the client.
        """
        c_branch_name = request.branch.name
        c_ordered_commits = request.commits

        try:
            s_history = summarize.list_history(
                refenv=self.env.refenv,
                branchenv=self.env.branchenv,
                branch_name=c_branch_name)
        except ValueError:
            msg = f'BRANCH NOT EXIST. Name: {c_branch_name}'
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(msg)
            err = hangar_service_pb2.ErrorProto(code=5, message=msg)
            reply = hangar_service_pb2.FindMissingCommitsReply(error=err)
            return reply

        s_orderset = set(s_history['order'])
        c_orderset = set(c_ordered_commits)
        c_missing = list(s_orderset.difference(c_orderset))   # only difference to PushFindMissingCommits

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        if len(c_missing) == 0:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=s_history['head'])
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
        else:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=s_history['head'])
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
            reply.commits.extend(c_missing)

        return reply

    def PushFindMissingCommits(self, request, context):
        """Determine commit digests existing on the client which are not present on the server.
        """
        c_branch_name = request.branch.name
        c_head_commit = request.branch.commit
        c_ordered_commits = request.commits

        s_commits = commiting.list_all_commits(self.env.refenv)
        s_orderset = set(s_commits)
        c_orderset = set(c_ordered_commits)
        s_missing = list(c_orderset.difference(s_orderset))  # only difference to FetchFindMissingCommits

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        if len(s_missing) == 0:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=c_head_commit)
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
        else:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=c_head_commit)
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
            reply.commits.extend(s_missing)

        return reply

    def FetchFindMissingHashRecords(self, request_iterator, context):
        """Determine data tensor hash records existing on the server and not on the client.
        """
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                hBytes, offset = bytearray(request.total_byte_size), 0
            size = len(request.hashs)
            hBytes[offset: offset + size] = request.hashs
            offset += size

        uncompBytes = blosc.decompress(hBytes)
        c_hashs_raw = chunks.deserialize_record_pack(uncompBytes)
        c_hashset = set([chunks.deserialize_ident(raw).digest for raw in c_hashs_raw])

        with tempfile.TemporaryDirectory() as tempD:
            tmpDF = os.path.join(tempD, 'test.lmdb')
            tmpDB = lmdb.open(path=tmpDF, **c.LMDB_SETTINGS)
            commiting.unpack_commit_ref(self.env.refenv, tmpDB, commit)
            s_hashes_schemas = queries.RecordQuery(tmpDB).data_hash_to_schema_hash()
            s_hashes = set(s_hashes_schemas.keys())
            tmpDB.close()

        c_missing = list(s_hashes.difference(c_hashset))
        c_hash_schemas_raw = [chunks.serialize_ident(c_mis, s_hashes_schemas[c_mis]) for c_mis in c_missing]
        raw_pack = chunks.serialize_record_pack(c_hash_schemas_raw)
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        response_pb = hangar_service_pb2.FindMissingHashRecordsReply
        cIter = chunks.missingHashIterator(commit, raw_pack, err, response_pb)
        yield from cIter

    def PushFindMissingHashRecords(self, request_iterator, context):
        """Determine data tensor hash records existing on the client and not on the server.
        """
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                hBytes, offset = bytearray(request.total_byte_size), 0
            size = len(request.hashs)
            hBytes[offset: offset + size] = request.hashs
            offset += size

        uncompBytes = blosc.decompress(hBytes)
        c_hashs_raw = chunks.deserialize_record_pack(uncompBytes)
        c_hashset = set([chunks.deserialize_ident(raw).digest for raw in c_hashs_raw])
        s_hashset = set(hashs.HashQuery(self.env.hashenv).list_all_hash_keys_raw())
        s_missing = c_hashset.difference(s_hashset)
        s_hashs_raw = [chunks.serialize_ident(s_mis, '') for s_mis in s_missing]
        raw_pack = chunks.serialize_record_pack(s_hashs_raw)

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        response_pb = hangar_service_pb2.FindMissingHashRecordsReply
        cIter = chunks.missingHashIterator(commit, raw_pack, err, response_pb)
        yield from cIter

    def FetchFindMissingSchemas(self, request, context):
        """Determine schema hash digest records existing on the server and not on the client.
        """
        commit = request.commit
        c_schemas = set(request.schema_digests)

        with tempfile.TemporaryDirectory() as tempD:
            tmpDF = os.path.join(tempD, 'test.lmdb')
            tmpDB = lmdb.open(path=tmpDF, **c.LMDB_SETTINGS)
            commiting.unpack_commit_ref(self.env.refenv, tmpDB, commit)
            s_schemas = set(queries.RecordQuery(tmpDB).schema_hashes())
            tmpDB.close()

        c_missing = list(s_schemas.difference(c_schemas))
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.FindMissingSchemasReply(commit=commit, error=err)
        reply.schema_digests.extend(c_missing)
        return reply

    def PushFindMissingSchemas(self, request, context):
        """Determine schema hash digest records existing on the client and not on the server.
        """
        commit = request.commit
        c_schemas = set(request.schema_digests)
        s_schemas = set(hashs.HashQuery(self.env.hashenv).list_all_schema_digests())
        s_missing = list(c_schemas.difference(s_schemas))

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.FindMissingSchemasReply(commit=commit, error=err)
        reply.schema_digests.extend(s_missing)
        return reply


def serve(hangar_path: str,
          overwrite: bool = False,
          *,
          channel_address: str = None,
          restrict_push: bool = None,
          username: str = None,
          password: str = None) -> tuple:
    """Start serving the GRPC server. Should only be called once.

    Raises:
        e: critical error from one of the workers.
    """

    # ------------------- Configure Server ------------------------------------

    server_dir = pjoin(hangar_path, c.DIR_HANGAR_SERVER)
    CFG = server_config(server_dir, create=False)
    serverCFG = CFG['SERVER_GRPC']
    enable_compression = serverCFG['enable_compression']
    if enable_compression == 'NoCompression':
        compression_val = grpc.Compression.NoCompression
    elif enable_compression == 'Deflate':
        compression_val = grpc.Compression.Deflate
    elif enable_compression == 'Gzip':
        compression_val = grpc.Compression.Gzip
    else:
        compression_val = grpc.Compression.NoCompression

    optimization_target = serverCFG['optimization_target']
    if channel_address is None:
        channel_address = serverCFG['channel_address']
    max_thread_pool_workers = int(serverCFG['max_thread_pool_workers'])
    max_concurrent_rpcs = int(serverCFG['max_concurrent_rpcs'])

    adminCFG = CFG['SERVER_ADMIN']
    if (restrict_push is None) and (username is None) and (password is None):
        admin_restrict_push = bool(int(adminCFG['restrict_push']))
        admin_username = adminCFG['username']
        admin_password = adminCFG['password']
    else:
        admin_restrict_push = restrict_push
        admin_username = username
        admin_password = password
    msg = 'PERMISSION ERROR: PUSH OPERATIONS RESTRICTED FOR CALLER'
    code = grpc.StatusCode.PERMISSION_DENIED
    interc = request_header_validator_interceptor.RequestHeaderValidatorInterceptor(
        admin_restrict_push, admin_username, admin_password, code, msg)

    # ---------------- Start the thread pool for the grpc server --------------

    grpc_thread_pool = futures.ThreadPoolExecutor(
        max_workers=max_thread_pool_workers,
        thread_name_prefix='grpc_thread_pool')
    server = grpc.server(
        thread_pool=grpc_thread_pool,
        maximum_concurrent_rpcs=max_concurrent_rpcs,
        options=[('grpc.optimization_target', optimization_target)],
        compression=compression_val,
        interceptors=(interc,))

    # ------------------- Start the GRPC server -------------------------------

    hangserv = HangarServer(server_dir, overwrite)
    hangar_service_pb2_grpc.add_HangarServiceServicer_to_server(hangserv, server)
    port = server.add_insecure_port(channel_address)
    if port == 0:
        server.stop(0.1)
        server.wait_for_termination(timeout=2)
        raise OSError(f'Unable to bind port, adddress {channel_address} already in use.')
    return (server, hangserv, channel_address)


if __name__ == '__main__':
    workdir = os.getcwd()
    print(workdir)
    serve(workdir)
