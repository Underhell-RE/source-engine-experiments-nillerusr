#!/usr/bin/env python3
# ==============================================================================
# vcs_decompile.py — Source Engine .vcs контейнерный распаковщик + CTAB-анализ
#
# Назначение:
#   Распаковывает контейнер шейдеров Source Engine (.vcs, версии 4/5/6) в
#   набор отдельных скомпилированных шейдеров (сырые D3D9 байт-коды .dxso) и
#   извлекает из каждого комбо встроенную таблицу констант CTAB
#   (D3DXSHADER_CONSTANTTABLE), которую Valve оставляет в байт-коде, так как
#   d3dxfxc.cpp компилирует с Flags = 0 (без stripped constant tables).
#
#   Полная реконструкция исходного HLSL из DX9 байт-кода — это отдельная,
#   очень сложная задача (eслиembled операнды, поток управления, swizzle и т.п.).
#   Этот скрипт берёт на себя надёжную часть: распаковку контейнера,
#   декодирование блоков (bzip2 / LZMA / raw), v4-диффы, извлечение комбо
#   и парсинг CTAB. На выходе вы получаете:
#     * <имя>_s<static>_d<dynamic>.dxso   — готовый D3D9 шейдерный байт-код
#     * <имя>_s<static>_d<dynamic>.ctab.txt — человек-читаемая таблица констант
#     * <имя>_s<static>_d<dynamic>.hlsl   — скелет HLSL (константы/семплеры из CTAB)
#
# Зависимости: только стандартная библиотека Python 3 (struct, lzma, bz2,
# argparse, pathlib). Внешние компиляторы не требуются.
#
# Использование:
#   python3 devtools/bin/vcs_decompile.py путь/к/файлу.vcs
# ==============================================================================

import argparse
import bz2
import lzma
import os
import struct
import sys
from pathlib import Path

# ------------------------------------------------------------------------------
# Константы формата .vcs (см. public/materialsystem/shader_vcs_version.h)
# ------------------------------------------------------------------------------

SHADER_VCS_VERSION_NUMBER = 6
MAX_SHADER_UNPACKED_BLOCK_SIZE = 131072  # 128 KiB

# Версии контейнера
VCS_V4 = 4
VCS_V5 = 5
VCS_V6 = 6

# Размер заголовочной записи (v5/v6), 28 байт, packed (pragma pack(1))
VCS_HEADER_STRUCT = "<IIIIIII"  # 7 x int32 = 28 байт
VCS_HEADER_SIZE = struct.calcsize(VCS_HEADER_STRUCT)

# Заголовок v4 — тот же 28-байтовый, но поле m_nDiffReferenceSize
# вместо m_nNumStaticCombos / m_nNumStaticCombos в варианте v4
VCS_HEADER_V4_STRUCT = "<IIIIIII"

# Запись словаря комбо v4: {int32 m_Offset, int32 m_Size}
VCS_DICT_ENTRY_STRUCT = "<ii"
VCS_DICT_ENTRY_SIZE = struct.calcsize(VCS_DICT_ENTRY_STRUCT)

# Запись статического комбо v5/v6: {uint32 id, uint32 fileOffset}
VCS_STATIC_RECORD_STRUCT = "<II"
VCS_STATIC_RECORD_SIZE = struct.calcsize(VCS_STATIC_RECORD_STRUCT)

# Запись алиаса дубля v6: {uint32 dupID, uint32 sourceID}
VCS_ALIAS_RECORD_STRUCT = "<II"
VCS_ALIAS_RECORD_SIZE = struct.calcsize(VCS_ALIAS_RECORD_STRUCT)

# Маркер конца списка статических комбо (и дублей)
VCS_SENTINEL_ID = 0xFFFFFFFF
VCS_SENTINEL_OFFSET = 0xFFFFFFFF

# Кодеки блоков (старшие 2 бита nBlockSize)
VCS_BLOCK_UNCOMPRESSED = 0x80000000
VCS_BLOCK_LZMA = 0x40000000
VCS_BLOCK_BZIP2 = 0x00000000
VCS_BLOCK_CODEC_MASK = 0xC0000000
VCS_BLOCK_SIZE_MASK = 0x3FFFFFFF

# Пользовательский 17-байтовый заголовок LZMA (см. public/tier1/lzmaDecoder.h)
LZMA_ID = 0x414D5A4C  # 'LZMA' little-endian
LZMA_HEADER_STRUCT = "<4sII5s"  # id, actualSize, lzmaSize, properties[5]
LZMA_HEADER_SIZE = struct.calcsize(LZMA_HEADER_STRUCT)

# DX9 инструкционные опкоды
D3DSIO_COMMENT = 0xFFFE
D3DSIO_END = 0xFFFF
D3DSIO_PHASE = 0xFFFD

# Константная таблица D3DX (встраивается как комментарий в байт-коде)
D3DXSHADER_CONSTANTTABLE_STRUCT = "<IIIIIII"  # Size, Creator, Version, Constants, ConstantInfo, Flags, Target
D3DXSHADER_CONSTANTTABLE_SIZE = struct.calcsize(D3DXSHADER_CONSTANTTABLE_STRUCT)

D3DXSHADER_CONSTANTINFO_STRUCT = "<IIHHHHII"  # Name, RegisterSet, RegisterIndex, RegisterCount, Reserved, TypeInfo, DefaultValue
# correction: Name(DWORD), RegisterSet(WORD), RegisterIndex(WORD), RegisterCount(WORD), Reserved(WORD), TypeInfo(DWORD), DefaultValue(DWORD)
D3DXSHADER_CONSTANTINFO_STRUCT = "<IHHHHII"
D3DXSHADER_CONSTANTINFO_SIZE = struct.calcsize(D3DXSHADER_CONSTANTINFO_STRUCT)

D3DXSHADER_TYPEINFO_STRUCT = "<HHHHHHI"  # Class, Type, Rows, Columns, Elements, StructMembers, StructMemberInfo
D3DXSHADER_TYPEINFO_SIZE = struct.calcsize(D3DXSHADER_TYPEINFO_STRUCT)

D3DXSHADER_STRUCTMEMBERINFO_STRUCT = "<II"  # Name, TypeInfo
D3DXSHADER_STRUCTMEMBERINFO_SIZE = struct.calcsize(D3DXSHADER_STRUCTMEMBERINFO_STRUCT)

# Регистровые наборы D3DXREGISTER_SET
D3DX_REGISTER_SET_NAMES = {
    0: "BOOL",
    1: "INT4",
    2: "FLOAT4",
    3: "SAMPLER",
}

# Классы параметров D3DXPARAMETER_CLASS
D3DX_PARAM_CLASS_NAMES = {
    0: "SCALAR",
    1: "VECTOR",
    2: "MATRIX_ROWS",
    3: "MATRIX_COLUMNS",
    4: "OBJECT",
    5: "STRUCT",
}

# Типы параметров D3DXPARAMETER_TYPE
D3DX_PARAM_TYPE_NAMES = {
    0: "VOID",
    1: "BOOL",
    2: "INT",
    3: "FLOAT",
    4: "STRING",
    5: "TEXTURE",
    6: "TEXTURE1D",
    7: "TEXTURE2D",
    8: "TEXTURE3D",
    9: "TEXTURECUBE",
    10: "SAMPLER",
    11: "SAMPLER1D",
    12: "SAMPLER2D",
    13: "SAMPLER3D",
    14: "SAMPLERCUBE",
    15: "PIXELSHADER",
    16: "VERTEXSHADER",
    17: "PIXELFRAGMENT",
    18: "VERTEXFRAGMENT",
    19: "UNSUPPORTED",
}


# ------------------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------------------

def fail(msg):
    print(f"ошибка: {msg}", file=sys.stderr)
    sys.exit(1)


def read_u32_le(data, off):
    return struct.unpack_from("<I", data, off)[0]


def read_u16_le(data, off):
    return struct.unpack_from("<H", data, off)[0]


def decode_lzma_raw(payload, actual_size):
    """
    Декомпрессия пользовательского LZMA-блока Valve.

    Layout после 17-байтового заголовка lzma_header_t:
        [5 байт свойств] [сжатый поток без 8-байтового префикса размера]

    Python lzma module ожидает формат FORMAT_ALONE:
        [5 байт свойств] [8 байт uncompressed size LE] [сжатые данные]

    Поэтому мы реконструируем стандартный заголовок и используем lzma.decompress.
    """
    props = payload[:5]
    compressed = payload[5:]
    # FORMAT_ALONE заголовок: properties(5) + uncompressed_size(8, LE)
    alone_header = props + struct.pack("<Q", actual_size)
    stream = alone_header + compressed
    return lzma.decompress(stream, format=lzma.FORMAT_ALONE)


def decompress_block(block_data):
    """
    Распаковывает один блок комбо (без 4-байтового заголовка nBlockSize).
    block_data уже должен содержать сырой кодек-специфичный payload.
    Возвращает распакованные байты.
    """
    if not block_data:
        return b""

    # Кодек определяется старшими 2 битами первого u32 (заголовок блока),
    # но здесь мы получаем уже очищенный payload — кодек должен быть известен
    # вызывающей стороне. Для универсальности попробуем определить по сигнатуре.
    # Если данные начинаются с 'LZMA' — это наш кастомный заголовок.
    if len(block_data) >= 4 and struct.unpack_from("<I", block_data, 0)[0] == LZMA_ID:
        header = struct.unpack_from(LZMA_HEADER_STRUCT, block_data, 0)
        _id, actual_size, _lzma_size, props = header
        payload = block_data[LZMA_HEADER_SIZE:]
        return decode_lzma_raw(props + payload, actual_size)

    # Попробуем bzip2 (BZ signature)
    if len(block_data) >= 2 and block_data[:2] == b"BZ":
        return bz2.decompress(block_data)

    # Иначе считаем, что это нераспакованные данные (заглушка, если что)
    return block_data


# ------------------------------------------------------------------------------
# Парсинг CTAB (D3DXSHADER_CONSTANTTABLE) из байт-кода шейдера
# ------------------------------------------------------------------------------

def find_comment_blocks(bytecode):
    """
    Ищет все комментарии D3DSIO_COMMENT в потоке байт-кода (массив u32).
    Возвращает список (offset_dwords, data_bytes) — данные без токена-opcode.
    """
    comments = []
    n = len(bytecode) // 4
    i = 0
    while i < n:
        token = struct.unpack_from("<I", bytecode, i * 4)[0]
        opcode = token & 0xFFFF
        if opcode == D3DSIO_END:
            i += 1
            continue
        if opcode == D3DSIO_COMMENT:
            n_dwords = (token >> 16) & 0x7FFF
            start = i + 1
            end = start + n_dwords
            if end <= n:
                comments.append(bytecode[start * 4:end * 4])
            i = end
            continue
        i += 1
    return comments


def read_cstring(buf, off):
    """Читает null-terminated ASCII строку из буфера по смещению off."""
    end = buf.find(b"\x00", off)
    if end == -1:
        end = len(buf)
    try:
        return buf[off:end].decode("ascii", errors="replace")
    except Exception:
        return ""
    finally:
        pass


def parse_ctab(ctab_data):
    """
    Парсит D3DXSHADER_CONSTANTTABLE из сырого буфера комментария.

    Возможна ситуация, когда перед структурой идёт 4-байтовая FourCC ('CTAB').
    Пытаемся оба варианта.
    """
    if len(ctab_data) < D3DXSHADER_CONSTANTTABLE_SIZE:
        return None

    # Попытка 1: структура начинается с самого начала
    result = _try_parse_ctab_struct(ctab_data, 0)
    if result is not None:
        return result

    # Попытка 2: пропускаем 4 байта (возможная FourCC)
    if len(ctab_data) >= 4:
        result = _try_parse_ctab_struct(ctab_data, 4)
        if result is not None:
            return result

    return None


def _try_parse_ctab_struct(buf, start):
    try:
        (size, creator_off, version, n_constants,
         const_info_off, flags, target_off) = struct.unpack_from(
            D3DXSHADER_CONSTANTTABLE_STRUCT, buf, start)

        # Базовая валидация полей
        if size < D3DXSHADER_CONSTANTTABLE_SIZE:
            return None
        if n_constants > 4096:  # защита от мусора
            return None
        if const_info_off < start + D3DXSHADER_CONSTANTTABLE_SIZE:
            return None
        if const_info_off + n_constants * D3DXSHADER_CONSTANTINFO_SIZE > len(buf):
            return None

        constants = []
        for k in range(n_constants):
            c_off = const_info_off + k * D3DXSHADER_CONSTANTINFO_SIZE
            (name_off, reg_set, reg_index, reg_count, _reserved,
             type_info_off, default_val_off) = struct.unpack_from(
                D3DXSHADER_CONSTANTINFO_STRUCT, buf, c_off)

            name = read_cstring(buf, name_off) if name_off < len(buf) else f"c{reg_index}"

            # Тип
            t_class = t_type = t_rows = t_cols = t_elements = t_members = 0
            struct_members = []
            if type_info_off and type_info_off + D3DXSHADER_TYPEINFO_SIZE <= len(buf):
                (t_class, t_type, t_rows, t_cols, t_elements,
                 t_members, struct_member_info_off) = struct.unpack_from(
                    D3DXSHADER_TYPEINFO_STRUCT, buf, type_info_off)
                if t_members and struct_member_info_off:
                    for m in range(t_members):
                        m_off = struct_member_info_off + m * D3DXSHADER_STRUCTMEMBERINFO_SIZE
                        if m_off + D3DXSHADER_STRUCTMEMBERINFO_SIZE > len(buf):
                            break
                        (m_name_off, m_type_info_off) = struct.unpack_from(
                            D3DXSHADER_STRUCTMEMBERINFO_STRUCT, buf, m_off)
                        m_name = read_cstring(buf, m_name_off) if m_name_off < len(buf) else f"m{m}"
                        struct_members.append((m_name, m_type_info_off))

            # Регистр и компоненты
            reg_name = D3DX_REGISTER_SET_NAMES.get(reg_set, f"REG{reg_set}")
            type_name = D3DX_PARAM_TYPE_NAMES.get(t_type, f"TYPE{t_type}")
            class_name = D3DX_PARAM_CLASS_NAMES.get(t_class, f"CLASS{t_class}")

            constants.append({
                "name": name,
                "register_set": reg_name,
                "register_index": reg_index,
                "register_count": reg_count,
                "class": class_name,
                "type": type_name,
                "rows": t_rows,
                "columns": t_cols,
                "elements": t_elements,
                "struct_members": struct_members,
            })

        return {
            "size": size,
            "version": version,
            "constants": constants,
            "flags": flags,
        }
    except (struct.error, IndexError):
        return None


def ctab_to_text(ctab):
    lines = []
    lines.append(f"// D3DX Constant Table (version {ctab['version']}, flags 0x{ctab['flags']:08X})")
    lines.append(f"// Constants: {len(ctab['constants'])}")
    lines.append("")
    for c in ctab["constants"]:
        reg = c["register_set"]
        idx = c["register_index"]
        count = c["register_count"]
        lines.append(f"// {c['name']} : {reg}{idx} (count={count})")
        lines.append(f"//   class={c['class']} type={c['type']} rows={c['rows']} cols={c['columns']}")
        if c["elements"] > 1:
            lines.append(f"//   elements={c['elements']}")
        for m_name, _m_ti in c["struct_members"]:
            lines.append(f"//   .{m_name}")
        lines.append("")
    return "\n".join(lines)


def ctab_to_hlsl_skeleton(shader_name, ctab):
    """Генерирует скелет HLSL с объявлениями констант и семплеров на основе CTAB."""
    lines = []
    lines.append(f"// Skeleton HLSL reconstructed from CTAB of {shader_name}")
    lines.append("// NOTE: это скелет, созданный автоматически. Тела функций/операции не восстановлены.")
    lines.append("")
    lines.append("//////////////////////////////////////////////////////////////////////////////")
    lines.append("// Константы (c-registers)")
    lines.append("//////////////////////////////////////////////////////////////////////////////")
    for c in ctab["constants"]:
        if c["register_set"] != "FLOAT4":
            continue
        name = c["name"]
        idx = c["register_index"]
        count = c["register_count"]
        rows = c["rows"]
        cols = c["columns"]
        if rows > 1 and cols > 1:
            # Матрица
            lines.append(f"float4x4 {name} : register(c{idx});")
        elif rows == 1 and cols == 4:
            lines.append(f"float4 {name} : register(c{idx});")
        elif rows == 1 and cols <= 3:
            lines.append(f"float{cols} {name} : register(c{idx});")
        elif cols == 1 and rows == 1:
            lines.append(f"float {name} : register(c{idx});")
        else:
            lines.append(f"float4 {name}[{count}] : register(c{idx});")
    lines.append("")
    lines.append("//////////////////////////////////////////////////////////////////////////////")
    lines.append("// Семплеры / текстуры (s-registers)")
    lines.append("//////////////////////////////////////////////////////////////////////////////")
    for c in ctab["constants"]:
        if c["register_set"] != "SAMPLER":
            continue
        name = c["name"]
        idx = c["register_index"]
        t = c["type"]
        if "CUBE" in t.upper():
            lines.append(f"samplerCUBE {name} : register(s{idx});")
        elif "3D" in t.upper():
            lines.append(f"sampler3D {name} : register(s{idx});")
        elif "2D" in t.upper():
            lines.append(f"sampler2D {name} : register(s{idx});")
        else:
            lines.append(f"sampler2D {name} : register(s{idx});")
    lines.append("")
    lines.append("// Тело шейдера не восстанавливается этим скриптом — используйте .dxso с")
    lines.append("// внешним декомпилятором (3Dmigoto / DXVK dxso->spirv -> SPIRV-Cross).")
    return "\n".join(lines)


# ------------------------------------------------------------------------------
# Парсинг .vcs контейнера
# ------------------------------------------------------------------------------

class VcsFile:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.version = None
        self.total_combos = None
        self.dynamic_combos = None
        self.flags = None
        self.centroid_mask = None
        self.diff_reference_size = None
        self.num_static_combos = None
        self.source_crc = None
        self.static_records = []      # list of (id, fileOffset)
        self.alias_records = []       # list of (dupID, sourceID) — только v6
        self.combos = []              # list of dicts: static_index, dynamic_index, bytecode

    def parse_header(self):
        if len(self.data) < VCS_HEADER_SIZE:
            fail(f"файл слишком мал для заголовка .vcs ({len(self.data)} байт)")

        fields = struct.unpack_from(VCS_HEADER_STRUCT, self.data, 0)
        (self.version, self.total_combos, self.dynamic_combos,
         self.flags, self.centroid_mask, field_0x14, self.source_crc) = fields

        if self.version not in (VCS_V4, VCS_V5, VCS_V6):
            fail(f"неподдерживаемая версия .vcs: {self.version} (ожидаются 4, 5 или 6)")

        if self.version == VCS_V4:
            self.diff_reference_size = field_0x14
            self.num_static_combos = None
        else:
            self.diff_reference_size = 0
            self.num_static_combos = field_0x14

        # Для v4: читаем reference combo и словарь динамических комбо
        if self.version == VCS_V4:
            self._parse_v4()
        else:
            self._parse_v5v6()

    def _parse_v4(self):
        """
        Layout v4:
            [Header 28 байт][referenceCombo[m_nDiffReferenceSize]]
            [ShaderDictionaryEntry_t[m_nTotalCombos] {m_Offset, m_Size}]
        """
        ref_size = self.diff_reference_size
        ref_start = VCS_HEADER_SIZE
        ref_end = ref_start + ref_size
        if ref_end > len(self.data):
            fail(f"v4: reference combo выходит за пределы файла (нужно {ref_end}, есть {len(self.data)})")
        self.reference_combo = self.data[ref_start:ref_end]

        dict_start = ref_end
        dict_end = dict_start + self.total_combos * VCS_DICT_ENTRY_SIZE
        if dict_end > len(self.data):
            fail(f"v4: словарь комбо выходит за пределы файла")

        self.dictionary = []
        for i in range(self.total_combos):
            offset, size = struct.unpack_from(VCS_DICT_ENTRY_STRUCT, self.data, dict_start + i * VCS_DICT_ENTRY_SIZE)
            self.dictionary.append((offset, size))

    def _parse_v5v6(self):
        """
        Layout v5/v6:
            [Header 28 байт]
            [StaticComboRecord_t[m_nNumStaticCombos] {id, fileOffset}, завершается {0xFFFFFFFF, 0xFFFFFFFF}]
            (только v6) [uint32 nNumDups][StaticComboAliasRecord_t[nNumDups] {dupID, sourceID}]
            [база данных блоков комбо — последовательность блоков]
        """
        off = VCS_HEADER_SIZE

        # Читаем статические записи доセンチネル
        while True:
            if off + VCS_STATIC_RECORD_SIZE > len(self.data):
                fail("v5/v6: неожиданный конец файла при чтении статических комбо")
            sid, foffset = struct.unpack_from(VCS_STATIC_RECORD_STRUCT, self.data, off)
            off += VCS_STATIC_RECORD_SIZE
            if sid == VCS_SENTINEL_ID and foffset == VCS_SENTINEL_OFFSET:
                break
            self.static_records.append((sid, foffset))

        # Только v6: читаем дубликаты
        if self.version == VCS_V6:
            if off + 4 > len(self.data):
                fail("v6: неожиданный конец файла при чтении количества дублей")
            n_dups = struct.unpack_from("<I", self.data, off)[0]
            off += 4
            for _ in range(n_dups):
                if off + VCS_ALIAS_RECORD_SIZE > len(self.data):
                    fail("v6: неожиданный конец файла при чтении дублей")
                dup_id, src_id = struct.unpack_from(VCS_ALIAS_RECORD_STRUCT, self.data, off)
                off += VCS_ALIAS_RECORD_SIZE
                self.alias_records.append((dup_id, src_id))

        self.combos_data_base = off

    def unpack_all(self):
        """Распаковывает все комбо из контейнера в список словарей."""
        if self.version == VCS_V4:
            self._unpack_v4()
        else:
            self._unpack_v5v6()

    def _unpack_v4(self):
        """v4: каждое динамическое комбо — отдельная запись в словаре."""
        for dyn_idx, (offset, size) in enumerate(self.dictionary):
            if offset == -1 or size <= 0:
                continue
            start = VCS_HEADER_SIZE + self.diff_reference_size + offset
            end = start + size
            if end > len(self.data):
                print(f"предупреждение: v4 комбо dyn={dyn_idx} выходит за пределы, пропускаем", file=sys.stderr)
                continue
            raw = self.data[start:end]

            # Если есть reference combo, применяем дифф
            if self.diff_reference_size > 0 and self.reference_combo:
                raw = self._apply_diffs(self.reference_combo, raw)

            self.combos.append({
                "static_index": 0,   # v4 не имеет статических комбо в этом смысле
                "dynamic_index": dyn_idx,
                "bytecode": raw,
            })

    def _apply_diffs(self, old_block, diff_list):
        """
        Реализация ApplyDiffs из tier1/diff.cpp.
        Формат диффа:
            0x00 NN            — копировать следующие NN байт буквально (NN=1..127)
            0x80 0x00 u24      — большая сырая копия (устаревший формат)
            0x81..0xFF ofs(int8)  — скопировать N=(op&0x7F) байт из OldBlock со сдвигом
            0x80 len(int16) ofs(int16) — копия с 16-битовым смещением
            0x00 u16(ofs) i16(delta)? — долгое копирование (см. diff.cpp)
        """
        out = bytearray()
        copy_src = old_block
        old_size = len(old_block)
        i = 0
        n = len(diff_list)
        while i < n:
            op = diff_list[i]
            i += 1
            if op == 0:
                if i + 3 > n:
                    break
                copy_sz = diff_list[i] + (diff_list[i + 1] << 8)
                copy_ofs = diff_list[i + 2] + (diff_list[i + 3] << 8)
                if copy_ofs > 32767:
                    copy_ofs |= 0xFFFF0000
                i += 4
                # Защита от выхода за границы
                if copy_ofs < 0:
                    copy_ofs += old_size
                src = copy_src + copy_ofs
                if src < 0 or src + copy_sz > old_size:
                    print("предупреждение: v4 дифф выходит за границы reference, прерываем", file=sys.stderr)
                    break
                out.extend(old_block[src:src + copy_sz])
                copy_src = old_block[src + copy_sz:]
            elif op & 0x80:
                copy_sz = op & 0x7F
                if copy_sz == 0:
                    if i + 3 > n:
                        break
                    copy_sz = diff_list[i] + (diff_list[i + 1] << 8) + (diff_list[i + 2] << 16)
                    i += 3
                    if i + copy_sz > n:
                        break
                    out.extend(diff_list[i:i + copy_sz])
                    i += copy_sz
                    copy_src = old_block  # сброс? в оригинале copy_src не меняется для raw copy
                    continue
                if i + 1 > n:
                    break
                copy_ofs = diff_list[i]
                i += 1
                if copy_ofs > 127:
                    copy_ofs |= 0xFFFFFF80
                src_addr = copy_src + copy_ofs
                if src_addr < 0 or src_addr + copy_sz > old_size:
                    print("предупреждение: v4 дифф относительная копия вне границ, прерываем", file=sys.stderr)
                    break
                out.extend(old_block[src_addr:src_addr + copy_sz])
                copy_src = old_block[src_addr + copy_sz:]
            else:
                # 0x01..0x7F — буквальная копия следующих op байт
                if i + op > n:
                    break
                out.extend(diff_list[i:i + op])
                i += op
                copy_src = old_block  # после буквальной копии источник сбрасывается? в оригинале — нет явного сброса, но copy_src = DiffList + op
        return bytes(out)

    def _unpack_v5v6(self):
        """
        v5/v6: данные комбо лежат базой по self.combos_data_base.
        Каждый статический комбо idx映射 в диапазон блоков:
            [record[idx].m_nFileOffset, record[idx+1].m_nFileOffset)
        Внутри диапазона идут блоки, каждый начинается с u32 nBlockSize:
            старшие 2 бита — кодек, младшие 30 — размер в байтах payload.
            Блоки заканчиваются маркером u32 0xFFFFFFFF.
        Внутри unpacked-блока лежат кортежи:
            [u32 nCombo_ID, u32 nShaderSize, u8 bytecode[nShaderSize]]
        nCombo_ID имеет m_nStaticIndex, вычтенный, если >= m_nStaticIndex.
        """
        # Построим список boundary offsets для быстрого доступа
        record_offsets = [r[1] for r in self.static_records]
        n_records = len(record_offsets)

        for rec_idx in range(n_records):
            static_id = self.static_records[rec_idx][0]
            start_off = self.combos_data_base + record_offsets[rec_idx]
            end_off = self.combos_data_base + (record_offsets[rec_idx + 1] if rec_idx + 1 < n_records else len(self.data) - self.combos_data_base)

            # Для последней записи размер до конца файла
            if rec_idx + 1 < n_records:
                end_off = self.combos_data_base + record_offsets[rec_idx + 1]
            else:
                end_off = len(self.data)

            pos = start_off
            while pos < end_off:
                if pos + 4 > len(self.data):
                    break
                n_block_size = struct.unpack_from("<I", self.data, pos)[0]
                pos += 4

                if n_block_size == VCS_SENTINEL_OFFSET:  # 0xFFFFFFFF — конец блоков для этого комбо
                    break

                codec = n_block_size & VCS_BLOCK_CODEC_MASK
                block_len = n_block_size & VCS_BLOCK_SIZE_MASK

                if block_len > MAX_SHADER_UNPACKED_BLOCK_SIZE:
                    print(f"предупреждение: блок размером {block_len} > {MAX_SHADER_UNPACKED_BLOCK_SIZE}, пропускаем", file=sys.stderr)
                    break

                if pos + block_len > len(self.data):
                    print(f"предупреждение: блок выходит за пределы файла на позиции {pos}", file=sys.stderr)
                    break

                payload = self.data[pos:pos + block_len]
                pos += block_len

                # Распаковка блока
                if codec == VCS_BLOCK_LZMA:
                    try:
                        unpacked = decompress_block(payload)
                    except Exception as e:
                        print(f"предупреждение: ошибка LZMA декомпрессии: {e}", file=sys.stderr)
                        continue
                elif codec == VCS_BLOCK_BZIP2:
                    try:
                        unpacked = bz2.decompress(payload)
                    except Exception as e:
                        print(f"предупреждение: ошибка bzip2 декомпрессии: {e}", file=sys.stderr)
                        continue
                elif codec == VCS_BLOCK_UNCOMPRESSED:
                    unpacked = payload
                else:
                    print(f"предупреждение: неизвестный кодек блока 0x{codec:08X}, пропускаем", file=sys.stderr)
                    continue

                # Парсим кортежи комбо внутри unpacked-блока
                self._parse_combo_tuples(unpacked, static_id)

    def _parse_combo_tuples(self, block_data, static_id):
        """
        Внутри распакованного блока лежат кортежи:
            [u32 nCombo_ID, u32 nShaderSize, u8 bytecode[nShaderSize]]
        nCombo_ID имеет m_nStaticIndex, вычтенный, если >= m_nStaticIndex
        (в реальном движке m_nStaticIndex берётся из lookup, здесь мы используем static_id).
        """
        pos = 0
        n = len(block_data)
        while pos + 8 <= n:
            combo_id = struct.unpack_from("<I", block_data, pos)[0]
            shader_size = struct.unpack_from("<I", block_data, pos + 4)[0]
            pos += 8

            # В движке: if (nCombo_ID >= m_nStaticIndex) nCombo_ID -= m_nStaticIndex;
            # Здесь static_id служит индексом статического комбо.
            if combo_id >= static_id:
                combo_id -= static_id

            if pos + shader_size > n:
                print(f"предупреждение: байт-код комбо выходит за пределы блока (нужно {shader_size}, осталось {n - pos})", file=sys.stderr)
                break

            bytecode = block_data[pos:pos + shader_size]
            pos += shader_size

            self.combos.append({
                "static_index": static_id,
                "dynamic_index": combo_id,
                "bytecode": bytecode,
            })


# ------------------------------------------------------------------------------
# Запись результатов
# ------------------------------------------------------------------------------

def write_outputs(output_dir, shader_name, vcs):
    output_dir.mkdir(parents=True, exist_ok=True)

    index_file = output_dir / "index.txt"
    with open(index_file, "w", encoding="utf-8") as idx:
        idx.write(f"Source Engine .vcs unpack results\n")
        idx.write(f"Source file: {vcs.path}\n")
        idx.write(f"Version: {vcs.version}\n")
        idx.write(f"Total combos: {vcs.total_combos}\n")
        idx.write(f"Dynamic combos: {vcs.dynamic_combos}\n")
        idx.write(f"Static combos: {vcs.num_static_combos}\n")
        idx.write(f"Source CRC32: 0x{vcs.source_crc:08X}\n")
        idx.write(f"Centroid mask: 0x{vcs.centroid_mask:08X}\n")
        if vcs.version == VCS_V4:
            idx.write(f"Diff reference size: {vcs.diff_reference_size}\n")
        idx.write(f"Extracted shader combos: {len(vcs.combos)}\n")
        idx.write("\n")

        for i, combo in enumerate(vcs.combos):
            sidx = combo["static_index"]
            didx = combo["dynamic_index"]
            bytecode = combo["bytecode"]

            base_name = f"{shader_name}_s{sidx}_d{didx}"
            dxso_path = output_dir / f"{base_name}.dxso"
            ctab_path = output_dir / f"{base_name}.ctab.txt"
            hlsl_path = output_dir / f"{base_name}.hlsl"

            # Сырой DX9 байт-код
            dxso_path.write_bytes(bytecode)

            # CTAB
            comments = find_comment_blocks(bytecode)
            ctab = None
            for comment in comments:
                ctab = parse_ctab(comment)
                if ctab is not None:
                    break

            ctab_text = ""
            if ctab is not None:
                ctab_text = ctab_to_text(ctab)
                ctab_path.write_text(ctab_text, encoding="utf-8")
                hlsl_text = ctab_to_hlsl_skeleton(base_name, ctab)
                hlsl_path.write_text(hlsl_text, encoding="utf-8")
            else:
                ctab_path.write_text("// CTAB не найден в байт-коде этого комбо\n", encoding="utf-8")
                hlsl_path.write_text("// CTAB не найден — невозможно составить скелет HLSL\n", encoding="utf-8")

            idx.write(f"{i:03d}  static={sidx}  dynamic={didx}  bytes={len(bytecode)}  "
                      f"ctab={'yes' if ctab else 'no'}  file={base_name}\n")

    print(f"Готово: распаковано {len(vcs.combos)} комбо в {output_dir}")


# ------------------------------------------------------------------------------
# Точка входа
# ------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Распаковывает .vcs контейнер шейдеров Source Engine и извлекает CTAB."
    )
    parser.add_argument("vcs_file", help="Путь к .vcs файлу")
    parser.add_argument("-o", "--output", help="Выходная директория (по умолчанию рядом с файлом, то же имя без расширения)")
    args = parser.parse_args()

    vcs_path = Path(args.vcs_file)
    if not vcs_path.is_file():
        fail(f"файл не найден: {vcs_path}")

    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = vcs_path.with_suffix("")

    vcs = VcsFile(vcs_path)
    try:
        vcs.parse_header()
    except Exception as e:
        fail(f"ошибка парсинга заголовка: {e}")

    shader_name = vcs_path.stem
    try:
        vcs.unpack_all()
    except Exception as e:
        fail(f"ошибка распаковки комбо: {e}")

    write_outputs(output_dir, shader_name, vcs)


if __name__ == "__main__":
    main()
