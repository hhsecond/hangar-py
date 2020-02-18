from .recordstructs cimport ColumnSchemaKey, \
    FlatColumnDataKey, \
    NestedColumnDataKey, \
    MetadataRecordKey, \
    DataRecordVal

from .recordstructs import ColumnSchemaKey, \
    FlatColumnDataKey, \
    NestedColumnDataKey, \
    MetadataRecordKey, \
    DataRecordVal


import ast


# ----------------------- Schema Record Parsers -------------------------------


cpdef bytes schema_record_count_start_range_key():
    return 's:'.encode()


cpdef bytes schema_db_key_from_column(str column, str layout):
    """column schema db formated key from name and layout.

    Parameters
    ----------
    column: str
        name of the column
    layout: str
        layout of the column schema ('flat', 'nested', etc.)
    """
    cdef str serial

    if layout == 'flat':
        serial = f's:{column}:f'
    elif layout == 'nested':
        serial = f's:{column}:n'
    else:
        raise ValueError(f'layout {layout} not valid')
    return serial.encode()


cpdef bytes schema_db_range_key_from_column_unknown_layout(str column):
    cdef str serial

    serial = f's:{column}:'
    return serial.encode()


cpdef ColumnSchemaKey schema_column_record_from_db_key(bytes raw):
    cdef str serial, column, layout

    serial = raw.decode()
    _, column, layout = serial.split(':')
    if layout == 'f':
        layout = 'flat'
    elif layout == 'n':
        layout = 'nested'
    else:
        raise ValueError(f'layout unknown for serial key {serial}')
    return ColumnSchemaKey(column, layout)


cpdef bytes schema_db_val_from_spec(dict schema):
    cdef str serial

    serial = repr(schema).replace(' ', '')
    return serial.encode()


cpdef dict schema_spec_from_db_val(bytes raw):
    cdef str serialized
    cdef dict schema

    serialized = raw.decode()
    schema = ast.literal_eval(serialized)
    return schema


cpdef bytes schema_hash_db_key_from_digest(str digest):
    return f's:{digest}'.encode()


# -------------------- Data Digest Record Value Parser -------------------------


cpdef DataRecordVal data_record_digest_val_from_db_val(bytes raw):
    """Convert and split a lmdb record value into data record val struct
    """
    cdef str serial

    serial = raw.decode()
    return DataRecordVal(serial)


cpdef bytes data_record_db_val_from_digest(str digest):
    """convert a data digest value spec into the appropriate lmdb record value
    """
    return digest.encode()


# -------------------------- flat parser --------------------------------------


cpdef bytes flat_data_column_record_start_range_key(str column):
    cdef str serial

    serial = f'f:{column}:'
    return serial.encode()


cpdef bytes flat_data_db_key_from_names(str column, sample):
    cdef str serial

    if isinstance(sample, int):
        serial = f'f:{column}:#{sample}'
    else:
        serial = f'f:{column}:{sample}'
    return serial.encode()


cpdef FlatColumnDataKey flat_data_record_from_db_key(bytes raw):
    cdef str serial, column, sample

    serial = raw.decode()
    _, column, sample = serial.split(':')
    return FlatColumnDataKey(column, sample)


# -------------------------- nested parser ------------------------------------


cpdef bytes nested_data_column_record_start_range_key(str column):
    cdef str serial

    serial = f'n:{column}:'
    return serial.encode()


cpdef bytes nested_data_db_key_from_names(str column, sample, subsample):
    cdef str serial

    if isinstance(sample, int):
        sample = f'#{sample}'
    if isinstance(subsample, int):
        subsample = f'#{subsample}'
    serial = f'n:{column}:{sample}:{subsample}'
    return serial.encode()


cpdef NestedColumnDataKey nested_data_record_from_db_key(bytes raw):
    cdef str serial, column, sample, subsample

    serial = raw.decode()
    _, column, sample, subsample = serial.split(':')
    return NestedColumnDataKey(column, sample, subsample)


# ------------------------- Metadata Tag Parsers ------------------------------


cpdef bytes metadata_range_key():
    """return the metadata db range counter key
    """
    return 'l:'.encode()


cpdef MetadataRecordKey metadata_record_raw_key_from_db_key(bytes raw):
    """Convert and split a lmdb record key & value into python objects
    """
    cdef str serial, key

    serial = raw.decode()
    _, key = serial.split(':')
    return MetadataRecordKey(key)


cpdef bytes metadata_record_db_key_from_raw_key(key):
    """converts a python metadata name into the appropriate lmdb key
    """
    cdef str serial

    if isinstance(key, int):
        serial = f'l:#{key}'
    else:
        serial = f'l:{key}'
    return serial.encode()


# ----------------------- dynamic parser selection ----------------------------


cpdef object dynamic_layout_data_record_from_db_key(bytes raw):
    if raw[0:2] == b'f:':
        res = flat_data_record_from_db_key(raw)
    elif raw[0:2] == b'n:':
        res = nested_data_record_from_db_key(raw)
    elif raw[0:2] == b'l:':
        res = metadata_record_raw_key_from_db_key(raw)
    elif raw[0:2] == b's:':
        res = schema_column_record_from_db_key(raw)
    else:
        raise ValueError(raw)
    return res


cpdef bytes dynamic_layout_data_record_db_start_range_key(ColumnSchemaKey column_record):
    cdef bytes res

    if column_record.layout == 'flat':
        res = flat_data_column_record_start_range_key(column_record.column)
    elif column_record.layout == 'nested':
        res = nested_data_column_record_start_range_key(column_record.column)
    else:
        raise ValueError(column_record)
    return res


#
# Data Hash parsing functions used to convert db key/val to raw pyhon obj
# -----------------------------------------------------------------------


cpdef bytes hash_schema_db_key_from_raw_key(str schema_hash):
    return f's:{schema_hash}'.encode()


cpdef bytes hash_data_db_key_from_raw_key(str data_hash):
    return f'h:{data_hash}'.encode()


cpdef str hash_schema_raw_key_from_db_key(bytes db_key):
    return db_key[2:].decode()



cpdef str hash_data_raw_key_from_db_key(bytes db_key):
    return db_key[2:].decode()


#
# Metadata/Label Hash parsing functions used to convert db key/val to raw pyhon obj
# ---------------------------------------------------------------------------------
#

cpdef bytes hash_meta_db_key_from_raw_key(str meta_hash):
    return f'h:{meta_hash}'.encode()


cpdef bytes hash_meta_db_val_from_raw_val(str meta_val):
    return meta_val.encode()


cpdef str hash_meta_raw_key_from_db_key(bytes db_key):
    return db_key.decode()[2:]


cpdef str hash_meta_raw_val_from_db_val(bytes db_val):
    return db_val.decode()
