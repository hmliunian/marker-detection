"""Decode the BFBS-described scalar/table/struct/vector types in legacy MCAP.

The wire layout is read from each file's embedded reflection schema. No generated
classes or imports from the reference repositories are needed. Unsupported schema
features fail explicitly instead of guessing offsets.
"""

import struct
from dataclasses import dataclass

from flatbuffers import number_types as nt
from flatbuffers.table import Table

_SCALARS = {
    2: nt.BoolFlags,
    3: nt.Int8Flags,
    4: nt.Uint8Flags,
    5: nt.Int16Flags,
    6: nt.Uint16Flags,
    7: nt.Int32Flags,
    8: nt.Uint32Flags,
    9: nt.Int64Flags,
    10: nt.Uint64Flags,
    11: nt.Float32Flags,
    12: nt.Float64Flags,
}


def _scalar(table: Table, slot: int, flags, default=0):
    offset = table.Offset(4 + slot * 2)
    return table.Get(flags, table.Pos + offset) if offset else default


def _text(table: Table, slot: int) -> str:
    offset = table.Offset(4 + slot * 2)
    return table.String(table.Pos + offset).decode("utf-8") if offset else ""


def _child(table: Table, slot: int) -> Table | None:
    offset = table.Offset(4 + slot * 2)
    return Table(table.Bytes, table.Indirect(table.Pos + offset)) if offset else None


def _children(table: Table, slot: int) -> list[Table]:
    offset = table.Offset(4 + slot * 2)
    if not offset:
        return []
    start, count = table.Vector(offset), table.VectorLen(offset)
    if start + count * 4 > len(table.Bytes):
        raise ValueError("Truncated BFBS object vector")
    return [Table(table.Bytes, table.Indirect(start + index * 4)) for index in range(count)]


@dataclass(frozen=True)
class _Field:
    name: str
    offset: int
    base_type: int
    element_type: int
    object_index: int
    default: object


@dataclass(frozen=True)
class _Object:
    name: str
    is_struct: bool
    size: int
    fields: tuple[_Field, ...]


class FlatbufferSchema:
    """An MCAP BFBS schema, verified against its declared root type."""

    def __init__(self, schema_data: bytes, expected_name: str):
        if len(schema_data) < 8 or schema_data[4:8] != b"BFBS":
            raise ValueError("Legacy schema must contain a BFBS identifier")
        self.name = expected_name
        self.objects: list[_Object] = []
        try:
            schema = Table(schema_data, int.from_bytes(schema_data[:4], "little"))
            root = _child(schema, 4)
            if root is None or _text(root, 0) != expected_name:
                raise ValueError("BFBS root type does not match the MCAP Schema.name")
            for obj in _children(schema, 0):
                fields = []
                for field in _children(obj, 1):
                    typ = _child(field, 1)
                    if typ is None:
                        raise ValueError("BFBS field is missing its type")
                    base = _scalar(typ, 0, nt.Uint8Flags)
                    optional = _scalar(field, 11, nt.BoolFlags)
                    if base not in (*_SCALARS, 13, 14, 15):
                        raise ValueError(f"Unsupported BFBS field type: {base}")
                    if optional or base in (13, 14, 15):
                        default = None
                    else:
                        floating = base in (11, 12)
                        default = _scalar(field, 5 if floating else 4, nt.Float64Flags if floating else nt.Int64Flags)
                    fields.append(
                        _Field(
                            _text(field, 0),
                            _scalar(field, 3, nt.Uint16Flags),
                            base,
                            _scalar(typ, 1, nt.Uint8Flags),
                            _scalar(typ, 2, nt.Int32Flags, -1),
                            default,
                        )
                    )
                self.objects.append(
                    _Object(
                        _text(obj, 0),
                        bool(_scalar(obj, 2, nt.BoolFlags)),
                        _scalar(obj, 4, nt.Int32Flags),
                        tuple(fields),
                    )
                )
            self.root_index = next(index for index, obj in enumerate(self.objects) if obj.name == expected_name)
        except (struct.error, IndexError, TypeError, StopIteration) as exc:
            raise ValueError(f"Invalid BFBS schema for {expected_name}") from exc

    def decode(self, data: bytes) -> dict:
        if len(data) < 4:
            raise ValueError(f"Truncated {self.name} payload")
        try:
            return self._object(data, int.from_bytes(data[:4], "little"), self.root_index, 0)
        except (struct.error, IndexError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Invalid {self.name} FlatBuffer payload") from exc

    def _object(self, data, position: int, index: int, depth: int) -> dict:
        if depth > 64 or position < 0 or position >= len(data):
            raise ValueError(f"Invalid {self.name} object offset or nesting")
        definition = self.objects[index]
        table = Table(data, position)
        result = {}
        for field in definition.fields:
            offset = field.offset if definition.is_struct else table.Offset(field.offset)
            if not definition.is_struct and not offset:
                result[field.name] = field.default
                continue
            location = position + offset
            if field.base_type != 14:
                result[field.name] = self._value(table, location, field.base_type, field.object_index, depth + 1)
                continue
            start, count = table.Vector(offset), table.VectorLen(offset)
            if field.element_type in _SCALARS:
                size = _SCALARS[field.element_type].bytewidth
            elif field.element_type == 15 and self.objects[field.object_index].is_struct:
                size = self.objects[field.object_index].size
            else:
                size = 4
            if size <= 0 or start + count * size > len(data):
                raise ValueError(f"Truncated {self.name}.{field.name} vector")
            if field.element_type == 4:
                result[field.name] = bytes(data[start : start + count])
            else:
                result[field.name] = [
                    self._value(table, start + number * size, field.element_type, field.object_index, depth + 1)
                    for number in range(count)
                ]
        return result

    def _value(self, table: Table, location: int, base_type: int, object_index: int, depth: int):
        if base_type in _SCALARS:
            return table.Get(_SCALARS[base_type], location)
        if base_type == 13:
            return table.String(location).decode("utf-8")
        if base_type == 15:
            if not 0 <= object_index < len(self.objects):
                raise ValueError("BFBS object index is out of range")
            definition = self.objects[object_index]
            position = location if definition.is_struct else table.Indirect(location)
            return self._object(table.Bytes, position, object_index, depth)
        raise ValueError(f"Unsupported BFBS element type: {base_type}")
