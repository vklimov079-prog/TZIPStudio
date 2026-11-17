import os
import re
import io
import base64
import shutil
import zipfile
import tempfile
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox

SET_SUPPORTED_EXTS = {".txt", ".cfg", ".html", ".js", ".json", ".dat"}


def read_text_smart(file_path: str) -> str:
    """Читает текстовый файл в любой кодировке (UTF-8, UTF-8-BOM, UTF-16, CP1251)."""
    with open(file_path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def sanitize_set_name(filename: str) -> str:
    """Формирует имя сета из имени файла без расширения, очищая недопустимые символы."""
    base = os.path.splitext(os.path.basename(filename))[0]
    cleaned = re.sub(r'[^0-9A-Za-zА-Яа-яЁё_\-]', '_', base).strip("_")
    return cleaned if cleaned else "default_set"


def escape_storage_data(text: str) -> str:
    """Экранирует обратный слэш и закрывающую квадратную скобку внутри блока .storage[...]."""
    return text.replace("\\", "\\\\").replace("]", "\\]")


def unescape_storage_data(text: str) -> str:
    """Восстанавливает исходные символы из блока .storage[...]."""
    return text.replace("\\]", "]").replace("\\\\", "\\")


def _extract_bracket_inner(text: str, open_bracket_idx: int) -> tuple[str, int] | None:
    """
    Надёжно извлекает содержимое [...] начиная с позиции open_bracket_idx ('['),
    учитывая экранированные символы (\\] и \\\\) и вложенные скобки.
    Возвращает (содержимое, индекс_закрывающей_скобки).
    """
    if open_bracket_idx >= len(text) or text[open_bracket_idx] != "[":
        return None
    depth = 0
    escaped = False
    for i in range(open_bracket_idx, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[open_bracket_idx + 1:i], i
    return None


def _parse_single_get_target(args_raw: str) -> tuple[str, str, str | None]:
    """
    Универсальный парсер конструкции get[путь] с поддержкой слэша в конце
    (например: get[papka/text.txt\\] или get[papka/text.txt].getset("name")).
    Возвращает (путь, режим, имя_сета).
    """
    m = re.search(r'(?<![0-9A-Za-z_])get\[', args_raw)
    if not m:
        return "", "inline", None

    open_idx = m.end() - 1
    extracted = _extract_bracket_inner(args_raw, open_idx)
    if not extracted:
        return "", "inline", None

    raw_inside, close_idx = extracted
    clean_path = raw_inside.replace("\\\\", "\\").strip()
    tail = args_raw[close_idx + 1:].lstrip()

    getset_m = re.match(r'^\.getset\(\s*["\']([^"\']+)["\']\s*\)', tail)
    if getset_m:
        return clean_path, "getset", getset_m.group(1).strip()

    getdata_m = re.match(r'^\.getdata\(\s*\)', tail)
    if getdata_m:
        return clean_path, "getdata", None

    return clean_path, "get_plain", None


def parse_setpacks_from_body(body: str) -> dict:
    """Надёжно парсит блоки Setpack(имя).storage[данные] (включая пустые блоки)."""
    setpacks = {}
    header_pattern = re.compile(r'Setpack\(\s*([^)]+?)\s*\)\.storage\[', re.DOTALL)
    pos = 0
    while pos < len(body):
        m = header_pattern.search(body, pos)
        if not m:
            break
        sp_name = m.group(1).strip().strip('"').strip("'")
        open_idx = m.end() - 1
        extracted = _extract_bracket_inner(body, open_idx)
        if not extracted:
            pos = m.end()
            continue
        sp_data, close_idx = extracted
        if sp_data.startswith("\r\n"):
            sp_data = sp_data[2:]
        elif sp_data.startswith("\n"):
            sp_data = sp_data[1:]
        if sp_data.endswith("\r\n"):
            sp_data = sp_data[:-2]
        elif sp_data.endswith("\n"):
            sp_data = sp_data[:-1]
        setpacks[sp_name] = unescape_storage_data(sp_data)
        pos = close_idx + 1
    return setpacks


def _remove_setpack_blocks(body: str) -> str:
    """Удаляет все блоки Setpack(...).storage[...] из текста перед парсингом File(...)."""
    header_pattern = re.compile(r'Setpack\(\s*[^)]+?\s*\)\.storage\[', re.DOTALL)
    out = []
    pos = 0
    while pos < len(body):
        m = header_pattern.search(body, pos)
        if not m:
            out.append(body[pos:])
            break
        out.append(body[pos:m.start()])
        open_idx = m.end() - 1
        extracted = _extract_bracket_inner(body, open_idx)
        if not extracted:
            out.append(body[m.start():m.end()])
            pos = m.end()
        else:
            _, close_idx = extracted
            pos = close_idx + 1
    return "".join(out)


def _resolve_disk_source_path(raw_path: str, base_dir: str) -> str | None:
    """Ищет реальный файл на диске с учётом любых слэшей (/ или \\) в начале и конце пути."""
    if not raw_path:
        return None
    norm = raw_path.strip().replace("\\", "/")
    if os.path.isabs(norm) and os.path.isfile(norm):
        return norm
    stripped = norm.strip("/")
    if not stripped:
        return None
    candidates = [
        os.path.join(base_dir, stripped),
        os.path.join(base_dir, os.path.basename(stripped)),
        os.path.abspath(stripped),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def sync_all_getset_files_in_text(tzip_content: str, base_dir: str = ".") -> str:
    """
    Находит все конструкции get[путь].getset("имя_сета") в тексте .tzip.
    Если файл по пути существует на диске, автоматически считывает его содержимое и записывает
    в соответствующий блок Setpack(имя_сета).storage[ ... ].
    """
    header_pattern = re.compile(r'(?<![0-9A-Za-z_])get\[')
    pos = 0
    pairs = []
    while pos < len(tzip_content):
        m = header_pattern.search(tzip_content, pos)
        if not m:
            break
        open_idx = m.end() - 1
        extracted = _extract_bracket_inner(tzip_content, open_idx)
        if not extracted:
            pos = m.end()
            continue
        raw_inside, close_idx = extracted
        tail = tzip_content[close_idx + 1:].lstrip()
        gs_m = re.match(r'^\.getset\(\s*["\']([^"\']+)["\']\s*\)', tail)
        if gs_m:
            rel_path = raw_inside.replace("\\\\", "\\").strip()
            set_name = gs_m.group(1).strip()
            pairs.append((rel_path, set_name))
        pos = close_idx + 1

    if not pairs:
        return tzip_content

    updated_text = tzip_content
    for rel_path, set_name in pairs:
        full_src = _resolve_disk_source_path(rel_path, base_dir)
        if not full_src:
            continue

        file_data = read_text_smart(full_src)
        safe_data = escape_storage_data(file_data)
        new_sp_block = f"Setpack({set_name}).storage[\n{safe_data}\n]"

        sp_block_pattern = re.compile(
            r'Setpack\(\s*["\']?' + re.escape(set_name) + r'["\']?\s*\)\.storage\[(?:\\.|[^\\\]])*\]',
            re.DOTALL
        )
        if sp_block_pattern.search(updated_text):
            updated_text = sp_block_pattern.sub(lambda _: new_sp_block, updated_text, count=1)
        else:
            updated_text = updated_text.rstrip() + "\n\n" + new_sp_block + "\n"

    return updated_text


def parse_tzip_blocks(content: str, base_dir: str = "."):
    lines = content.splitlines()
    non_empty_lines = [ln for ln in lines if ln.strip()]
    if not non_empty_lines or not non_empty_lines[0].strip().startswith("ziptext "):
        raise ValueError("Неверный синтаксис .tzip: первая строка должна начинаться с 'ziptext <имя_файла>'")
    
    first_idx = lines.index(non_empty_lines[0])
    archive_title = lines[first_idx].strip()[len("ziptext "):].strip()
    body = "\n".join(lines[first_idx + 1:])
    
    # 0. Парсинг всех блоков Subzip.data { ... }
    subzips = []
    subzip_pattern = re.compile(r'Subzip\.data\s*\{(.*?)\}', re.DOTALL)
    for sz_match in subzip_pattern.finditer(body):
        raw_inner = sz_match.group(1).strip()
        if raw_inner.startswith("get["):
            extracted = _extract_bracket_inner(raw_inner, 3)
            if extracted and extracted[1] == len(raw_inner) - 1:
                zip_rel_path = extracted[0].replace("\\\\", "\\").strip()
                full_zip_path = _resolve_disk_source_path(zip_rel_path, base_dir)
                if full_zip_path:
                    with open(full_zip_path, "rb") as zf_file:
                        b64_str = base64.b64encode(zf_file.read()).decode("ascii")
                    subzips.append({
                        "source": os.path.basename(zip_rel_path.replace("\\", "/").strip("/")),
                        "b64": b64_str
                    })
                continue

        cleaned_b64 = "".join(raw_inner.split())
        if cleaned_b64:
            subzips.append({
                "source": "embedded",
                "b64": cleaned_b64
            })

    body_without_subzip = subzip_pattern.sub("", body)

    # 1. Парсинг Setpack(*название*).storage[*данные*]
    setpacks = parse_setpacks_from_body(body_without_subzip)
    body_for_files = _remove_setpack_blocks(body_without_subzip)

    # 2. Парсинг File(...) { ... }.conv
    entries = []
    file_with_body_pattern = re.compile(
        r'File\(([^)]*)\)\s*\{(.*?)\}(\.conv)?\s*(?://\s*END_FILE)?',
        re.DOTALL
    )
    file_no_body_pattern = re.compile(
        r'File\(([^)]*)\)(\.conv)?\s*(?://\s*END_FILE)?',
        re.DOTALL
    )

    matched_spans = []
    raw_file_matches = []

    for m in file_with_body_pattern.finditer(body_for_files):
        matched_spans.append(m.span())
        raw_file_matches.append((m.start(), m.group(1).strip(), m.group(2), bool(m.group(3))))

    for m in file_no_body_pattern.finditer(body_for_files):
        s, e = m.span()
        if any(ms <= s and e <= me for ms, me in matched_spans):
            continue
        raw_file_matches.append((m.start(), m.group(1).strip(), None, bool(m.group(2))))

    raw_file_matches.sort(key=lambda x: x[0])

    for _, args_raw, inline_body, has_conv in raw_file_matches:
        if not args_raw and inline_body is None:
            continue
            
        arch_subpath = ""
        arch_kw = re.search(r'(?<![0-9A-Za-z_])archive\[', args_raw)
        if arch_kw:
            arch_ext = _extract_bracket_inner(args_raw, arch_kw.end() - 1)
            if arch_ext:
                arch_subpath = arch_ext[0].replace("\\\\", "\\").strip().strip("/\\")

        get_source_path, mode, set_name = _parse_single_get_target(args_raw)
        clean_get_path = get_source_path.replace("\\", "/").strip("/") if get_source_path else ""

        filename = None
        if clean_get_path:
            filename = os.path.basename(clean_get_path)
        elif set_name:
            filename = set_name if "." in set_name else f"{set_name}.txt"
        else:
            cleaned = re.sub(r'archive\[(?:\\.|[^\\\]])*\]', '', args_raw).strip(" ,")
            str_match = re.search(r'["\']([^"\']+)["\']', cleaned)
            if str_match:
                filename = str_match.group(1)
        if not filename:
            filename = f"file_{len(entries) + 1}.txt"
            
        resolved_text = ""
        has_setpack_data = bool(set_name and set_name in setpacks)

        if has_setpack_data:
            resolved_text = setpacks[set_name]
        elif inline_body is not None:
            resolved_text = inline_body.strip("\r\n")
        elif get_source_path:
            full_src = _resolve_disk_source_path(get_source_path, base_dir)
            if full_src:
                resolved_text = read_text_smart(full_src)
            else:
                stem_guess = sanitize_set_name(filename)
                if stem_guess in setpacks:
                    resolved_text = setpacks[stem_guess]
                elif filename in setpacks:
                    resolved_text = setpacks[filename]
                else:
                    resolved_text = ""
                
        entries.append({
            "archive_subpath": arch_subpath,
            "get_source_path": clean_get_path,
            "set_name": set_name or "",
            "mode": mode,
            "filename": filename,
            "has_conv": has_conv,
            "content": resolved_text
        })
        
    return archive_title, entries, setpacks, subzips


def format_subzip_block(zip_bytes: bytes) -> str:
    raw_b64 = base64.b64encode(zip_bytes).decode("ascii")
    wrapped_b64 = "\n".join(raw_b64[i:i+76] for i in range(0, len(raw_b64), 76))
    return f"Subzip.data {{\n{wrapped_b64}\n}}"


def ensure_ziptext_header(tzip_content: str, current_file_name: str = "*этот файл*") -> str:
    """Гарантирует, что первая строка содержит ziptext <имя_текущего_файла>."""
    lines = tzip_content.splitlines()
    if lines and lines[0].strip().startswith("ziptext "):
        lines[0] = f"ziptext {current_file_name}"
        return "\n".join(lines)
    if tzip_content.strip():
        return f"ziptext {current_file_name}\n\n" + tzip_content.lstrip()
    return f"ziptext {current_file_name}\n"


def pack_external_zip_to_tzip(tzip_content: str, zip_file_path: str, current_file_name: str = "*этот файл*") -> tuple:
    if not os.path.isfile(zip_file_path):
        raise FileNotFoundError(f"ZIP-файл не найден: {zip_file_path}")
    if not zipfile.is_zipfile(zip_file_path):
        raise ValueError("Выбранный файл не является корректным ZIP-архивом!")

    with open(zip_file_path, "rb") as f:
        zip_bytes = f.read()

    zip_stem = os.path.splitext(os.path.basename(zip_file_path))[0]
    added_blocks = []
    report_items = []
    used_set_names = set(re.findall(r'Setpack\(\s*([^)]+?)\s*\)', tzip_content))

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            arc_path = info.filename.replace("\\", "/").strip("/")
            base_name = os.path.basename(arc_path)
            ext = os.path.splitext(base_name)[1].lower()
            report_items.append(arc_path)

            if ext in SET_SUPPORTED_EXTS:
                raw_data = zf.read(info.filename)
                text_data = None
                for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1251"):
                    try:
                        text_data = raw_data.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if text_data is None:
                    text_data = raw_data.decode("utf-8", errors="replace")

                sub_folder = os.path.dirname(arc_path)
                archive_rel = f"{zip_stem}/{sub_folder}".strip("/") if sub_folder else zip_stem
                get_rel = f"{archive_rel}/{base_name}"

                base_set_name = sanitize_set_name(base_name)
                set_name = base_set_name
                counter = 2
                while set_name in used_set_names:
                    set_name = f"{base_set_name}_{counter}"
                    counter += 1
                used_set_names.add(set_name)

                safe_storage = escape_storage_data(text_data)
                added_blocks.append(
                    f'File( archive[{archive_rel}], get[{get_rel}].getset("{set_name}") ).conv // END_FILE\n\n'
                    f'Setpack({set_name}).storage[\n{safe_storage}\n]'
                )

    subzip_block = format_subzip_block(zip_bytes)
    base_clean = ensure_ziptext_header(tzip_content.rstrip(), current_file_name=current_file_name).rstrip()
    parts = [base_clean]
    if added_blocks:
        parts.append("\n\n".join(added_blocks))
    parts.append(subzip_block)

    updated_tzip = "\n\n".join(parts) + "\n"
    return updated_tzip, report_items


def add_single_file_as_set_to_tzip(
    tzip_content: str,
    file_path: str,
    tzip_path: str = None,
    custom_rel_path: str = None
) -> tuple:
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    base_name = os.path.basename(file_path)
    ext = os.path.splitext(base_name)[1].lower()
    if ext not in SET_SUPPORTED_EXTS:
        raise ValueError(f"Неподдерживаемое расширение '{ext}'. Разрешены: {', '.join(sorted(SET_SUPPORTED_EXTS))}")

    if tzip_path:
        tzip_dir = os.path.dirname(os.path.abspath(tzip_path))
        current_tzip_name = os.path.basename(tzip_path)
    else:
        tzip_dir = os.getcwd()
        current_tzip_name = "*этот файл*"

    abs_file = os.path.abspath(file_path)

    if custom_rel_path:
        rel_full = custom_rel_path.replace("\\", "/").strip("/")
    else:
        try:
            rel_full = os.path.relpath(abs_file, tzip_dir).replace("\\", "/").strip("/")
        except ValueError:
            rel_full = ""

        if not rel_full or rel_full.startswith("..") or not tzip_path:
            parent_folder = os.path.basename(os.path.dirname(abs_file))
            if parent_folder:
                rel_full = f"{parent_folder}/{base_name}"
            else:
                rel_full = base_name

    archive_dir = os.path.dirname(rel_full).strip("/")
    get_path = rel_full.strip("/")

    used_set_names = set(re.findall(r'Setpack\(\s*([^)]+?)\s*\)', tzip_content))
    base_set_name = sanitize_set_name(base_name)
    set_name = base_set_name
    counter = 2
    while set_name in used_set_names:
        set_name = f"{base_set_name}_{counter}"
        counter += 1

    file_data = read_text_smart(abs_file)
    safe_storage = escape_storage_data(file_data)
    block = (
        f'File( archive[{archive_dir}], get[{get_path}].getset("{set_name}") ).conv // END_FILE\n\n'
        f'Setpack({set_name}).storage[\n{safe_storage}\n]'
    )
    base_clean = ensure_ziptext_header(tzip_content.rstrip(), current_file_name=current_tzip_name).rstrip()
    updated_tzip = base_clean + "\n\n" + block + "\n"
    return updated_tzip, set_name, get_path


def convert_existing_zip_into_tzip_file(zip_file_path: str, output_tzip_path: str = None) -> tuple:
    if not os.path.isfile(zip_file_path):
        raise FileNotFoundError(f"ZIP-файл не найден: {zip_file_path}")
    if not zipfile.is_zipfile(zip_file_path):
        raise ValueError("Выбранный файл не является корректным ZIP-архивом!")

    if not output_tzip_path:
        output_tzip_path = os.path.splitext(zip_file_path)[0] + ".tzip"

    actual_tzip_filename = os.path.basename(output_tzip_path)
    out_lines = [f"ziptext {actual_tzip_filename}", ""]
    converted_report = []
    used_set_names = set()

    extra_text_exts = {
        ".py", ".xml", ".css", ".md", ".csv", ".ini", ".yaml", ".yml", ".log", ".bat", ".sh", ".sql", ".toml"
    }

    other_binary_items = []
    nested_zips = []

    with zipfile.ZipFile(zip_file_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            arc_path = info.filename.replace("\\", "/").strip("/")
            folder_part = os.path.dirname(arc_path)
            base_name = os.path.basename(arc_path)
            ext = os.path.splitext(base_name)[1].lower()
            data = zf.read(info.filename)

            if ext == ".zip" and zipfile.is_zipfile(io.BytesIO(data)):
                nested_zips.append((arc_path, data))
                converted_report.append(f"[Вложенный ZIP -> Subzip.data] {arc_path}")
                continue

            if ext in SET_SUPPORTED_EXTS:
                decoded_text = None
                for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1251"):
                    try:
                        decoded_text = data.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if decoded_text is None:
                    decoded_text = data.decode("utf-8", errors="replace")

                base_set_name = sanitize_set_name(base_name)
                set_name = base_set_name
                counter = 2
                while set_name in used_set_names:
                    set_name = f"{base_set_name}_{counter}"
                    counter += 1
                used_set_names.add(set_name)

                safe_storage = escape_storage_data(decoded_text)
                out_lines.append(f'File( archive[{folder_part}], get[{arc_path}].getset("{set_name}") ).conv // END_FILE')
                out_lines.append("")
                out_lines.append(f"Setpack({set_name}).storage[")
                out_lines.append(safe_storage)
                out_lines.append("]")
                out_lines.append("")
                converted_report.append(f"[SET: {set_name}] {arc_path}")
                continue

            is_text = ext in extra_text_exts
            decoded_text = None
            if is_text:
                try:
                    decoded_text = data.decode("utf-8")
                except UnicodeDecodeError:
                    is_text = False
            elif b"\x00" not in data:
                try:
                    decoded_text = data.decode("utf-8")
                    is_text = True
                except UnicodeDecodeError:
                    is_text = False

            if is_text and decoded_text is not None:
                safe_text = decoded_text.replace("} // END_FILE", "} // END_FILE_ESCAPED")
                if folder_part:
                    header = f'File( archive[{folder_part}], "{base_name}" ) {{'
                else:
                    header = f'File( "{base_name}" ) {{'
                out_lines.append(header)
                out_lines.append(safe_text.rstrip("\r\n"))
                out_lines.append("}.conv // END_FILE")
                out_lines.append("")
                converted_report.append(f"[TEXT -> File()] {arc_path}")
            else:
                other_binary_items.append((arc_path, data))
                converted_report.append(f"[BIN -> Subzip.data] {arc_path}")

    for arc_path, z_bytes in nested_zips:
        out_lines.append(f"// Вложенный архив: {arc_path}")
        out_lines.append(format_subzip_block(z_bytes))
        out_lines.append("")

    if other_binary_items:
        bin_buf = io.BytesIO()
        with zipfile.ZipFile(bin_buf, "w", compression=zipfile.ZIP_DEFLATED) as bzf:
            for arc_path, b_data in other_binary_items:
                bzf.writestr(arc_path, b_data)
        out_lines.append("// Бинарные ресурсы из исходного архива:")
        out_lines.append(format_subzip_block(bin_buf.getvalue()))
        out_lines.append("")

    final_tzip_text = "\n".join(out_lines).rstrip() + "\n"
    with open(output_tzip_path, "w", encoding="utf-8") as out_f:
        out_f.write(final_tzip_text)

    return output_tzip_path, final_tzip_text, converted_report


def resolve_get_subzips_in_text(tzip_content: str, base_dir: str = ".") -> str:
    subzip_pattern = re.compile(r'Subzip\.data\s*\{\s*get\[(.*?)\]\s*\}', re.DOTALL)

    def _replacer(match):
        zip_rel_path = match.group(1).replace("\\\\", "\\").strip()
        full_zip_path = _resolve_disk_source_path(zip_rel_path, base_dir)
        if not full_zip_path:
            raise FileNotFoundError(f"Архив для Subzip.data не найден: {zip_rel_path}")
        with open(full_zip_path, "rb") as f:
            return format_subzip_block(f.read())

    return subzip_pattern.sub(_replacer, tzip_content)


def extract_subzip_to_file(tzip_content: str, output_zip_path: str, base_dir: str = ".", index: int = 0) -> list:
    _, _, _, subzips = parse_tzip_blocks(tzip_content, base_dir=base_dir)
    if not subzips:
        raise ValueError("В этом .tzip файле нет ни одного блока Subzip.data { ... }!")
    if index >= len(subzips):
        index = 0

    zip_bytes = base64.b64decode(subzips[index]["b64"])
    with open(output_zip_path, "wb") as f:
        f.write(zip_bytes)

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        return zf.namelist()


def extract_tzip_content_to_dir(raw_content: str, target_dir: str, base_dir: str = ".") -> list:
    synced_content = sync_all_getset_files_in_text(raw_content, base_dir=base_dir)
    _, entries, setpacks, subzips = parse_tzip_blocks(synced_content, base_dir=base_dir)
    os.makedirs(target_dir, exist_ok=True)
    extracted = []
    used_sets = set()

    for item in entries:
        subpath = item["archive_subpath"].replace("/", os.sep).replace("\\", os.sep).strip(os.sep)
        out_folder = os.path.join(target_dir, subpath) if subpath else target_dir
        os.makedirs(out_folder, exist_ok=True)
        out_path = os.path.join(out_folder, item["filename"])
        restored_content = item["content"].replace("} // END_FILE_ESCAPED", "} // END_FILE")
        with open(out_path, "w", encoding="utf-8") as out:
            out.write(restored_content)
        extracted.append(out_path)
        if item["set_name"]:
            used_sets.add(item["set_name"])
            
    for sp_name, sp_data in setpacks.items():
        if sp_name not in used_sets:
            sp_filename = sp_name if "." in sp_name else f"{sp_name}.txt"
            out_path = os.path.join(target_dir, sp_filename)
            with open(out_path, "w", encoding="utf-8") as out:
                out.write(sp_data)
            extracted.append(out_path)

    for idx, sz in enumerate(subzips, start=1):
        zip_name = sz["source"] if sz["source"] != "embedded" else f"subzip_{idx}.zip"
        zip_out = os.path.join(target_dir, zip_name)
        with open(zip_out, "wb") as zout:
            zout.write(base64.b64decode(sz["b64"]))
        extracted.append(zip_out)

    return extracted


def load_tzip_copies(tzip_path: str, target_dir: str) -> list:
    if not os.path.isfile(tzip_path):
        raise FileNotFoundError(f"Файл не найден: {tzip_path}")
    raw = read_text_smart(tzip_path)
    base_dir = os.path.dirname(os.path.abspath(tzip_path))
    return extract_tzip_content_to_dir(raw, target_dir, base_dir=base_dir)


def unload_self_tzip(tzip_path: str, destination_dir: str, move_file: bool = False) -> str:
    if not os.path.isfile(tzip_path):
        raise FileNotFoundError(f"Файл не найден: {tzip_path}")
    with open(tzip_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    if not first_line.startswith("ziptext "):
        raise ValueError("Ошибка: файл не соответствует формату .tzip (нет 'ziptext <имя_файла>')")
    os.makedirs(destination_dir, exist_ok=True)
    dest_path = os.path.join(destination_dir, os.path.basename(tzip_path))
    if move_file:
        shutil.move(tzip_path, dest_path)
    else:
        shutil.copy2(tzip_path, dest_path)
    return dest_path


class TzipStudio1SApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("TZIPStudio(1S)")
        self.root.geometry("1120x740")
        self.root.configure(bg="#1e1e2e")
        self.current_tzip_path = None
        self._highlight_job = None
        self._build_ui()

    def _build_ui(self):
        header = tk.Label(
            self.root,
            text="TZIPStudio(1S) — Setpack(), .getset(), ZIP -> TZIP & Subzip.data {}",
            font=("Segoe UI", 13, "bold"),
            bg="#1e1e2e",
            fg="#cdd6f4"
        )
        header.pack(pady=(12, 4))

        sub = tk.Label(
            self.root,
            text='Заголовок: ziptext <имя_этого_файла.tzip> | Авто-сеты (.txt, .cfg, .html, .js, .json, .dat)',
            font=("Segoe UI", 9),
            bg="#1e1e2e",
            fg="#a6adc8"
        )
        sub.pack(pady=(0, 8))

        btn_frame = tk.Frame(self.root, bg="#1e1e2e")
        btn_frame.pack(fill="x", padx=16, pady=4)

        tk.Button(btn_frame, text="1. Очистить / Новый .tzip", command=self.clear_editor, bg="#89b4fa", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame, text="2. Вставить Setpack()", command=self.insert_setpack, bg="#f5c2e7", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame, text="3. Сохранить .tzip", command=self.save_tzip, bg="#a6e3a1", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame, text="4. Открыть .tzip", command=self.open_tzip, bg="#fab387", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame, text="★ Сделать из .zip -> .tzip", command=self.convert_zip_to_tzip_ui, bg="#f38ba8", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=5).pack(side="left", padx=3)

        btn_frame2 = tk.Frame(self.root, bg="#1e1e2e")
        btn_frame2.pack(fill="x", padx=16, pady=4)

        tk.Button(btn_frame2, text="5. Добавить zip", command=self.import_zip_ui, bg="#f9e2af", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame2, text="5.1 Добавить файл (сет)", command=self.add_set_file_ui, bg="#eba0ac", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame2, text="Импорт папки", command=self.import_folder_ui, bg="#f2cdcd", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame2, text="6. Выгрузить Subzip.data -> .zip", command=self.export_subzip_ui, bg="#74c7ec", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame2, text="7. Распаковать всё в папку", command=self.extract_copies_ui, bg="#cba6f7", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)
        tk.Button(btn_frame2, text="8. Выгрузить себя [Python]", command=self.unload_self_ui, bg="#94e2d5", fg="#11111b", font=("Segoe UI", 9, "bold"), relief="flat", padx=9, pady=5).pack(side="left", padx=3)

        self.editor = tk.Text(
            self.root,
            bg="#181825",
            fg="#cdd6f4",
            insertbackground="#f5e0dc",
            font=("Consolas", 10),
            relief="flat",
            padx=12,
            pady=12
        )
        self.editor.pack(fill="both", expand=True, padx=16, pady=8)

        # Контрастный лаймово-травяной тег для слэшей (/ и \) внутри [...] и вне {...}
        self.editor.tag_configure(
            "bracket_slash_lime",
            foreground="#84E838",
            font=("Consolas", 10, "bold")
        )

        # Чистый ярко-голубой цвет только для самих букв слова 'get' (без заливки фона между линиями букв)
        self.editor.tag_configure(
            "get_keyword_cyan",
            foreground="#00F5FF",
            font=("Consolas", 10, "bold")
        )

        self.editor.bind("<KeyRelease>", self._schedule_highlight)
        self.editor.bind("<<Paste>>", self._schedule_highlight)
        self.editor.bind("<<Cut>>", self._schedule_highlight)

        self.status_var = tk.StringVar(value="TZIPStudio(1S) готов к работе.")
        status_bar = tk.Label(self.root, textvariable=self.status_var, bg="#11111b", fg="#a6adc8", anchor="w", padx=12, pady=5, font=("Segoe UI", 9))
        status_bar.pack(fill="x", side="bottom")

        self.clear_editor()

    def _schedule_highlight(self, event=None):
        if self._highlight_job is not None:
            self.root.after_cancel(self._highlight_job)
        self._highlight_job = self.root.after(40, self._apply_syntax_highlight)

    def _apply_syntax_highlight(self):
        """
        1) Подсвечивает лаймово-травяным цветом (#84E838) символы '/' и '\\' внутри [...] и вне {...}.
        2) Подсвечивает ярко-голубым цветом (#00F5FF) сами буквы слова 'get' в конструкциях get[...] вне {...}.
        """
        self._highlight_job = None
        self.editor.tag_remove("bracket_slash_lime", "1.0", tk.END)
        self.editor.tag_remove("get_keyword_cyan", "1.0", tk.END)

        text = self.editor.get("1.0", "end-1c")
        if not text:
            return

        curly_depth = 0
        square_depth = 0
        escaped = False
        line = 1
        col = 0
        slash_indices = []
        get_spans = []

        for i, ch in enumerate(text):
            if ch == "\n":
                line += 1
                col = 0
                escaped = False
                continue

            if escaped:
                escaped = False
                col += 1
                continue

            if ch == "\\":
                if curly_depth == 0 and square_depth > 0:
                    slash_indices.append(f"{line}.{col}")
                escaped = True
                col += 1
                continue

            if ch == "{":
                curly_depth += 1
            elif ch == "}":
                if curly_depth > 0:
                    curly_depth -= 1
            elif ch == "[" and curly_depth == 0:
                if square_depth == 0 and i >= 3 and text[i-3:i] == "get":
                    prev_ok = (i == 3) or (not (text[i-4].isalnum() or text[i-4] == "_"))
                    if prev_ok and col >= 3:
                        get_spans.append((f"{line}.{col - 3}", f"{line}.{col}"))
                square_depth += 1
            elif ch == "]" and curly_depth == 0:
                if square_depth > 0:
                    square_depth -= 1
            elif ch == "/" and curly_depth == 0 and square_depth > 0:
                slash_indices.append(f"{line}.{col}")

            col += 1

        for idx in slash_indices:
            self.editor.tag_add("bracket_slash_lime", idx, f"{idx}+1c")

        for start_idx, end_idx in get_spans:
            self.editor.tag_add("get_keyword_cyan", start_idx, end_idx)

    def _get_current_tzip_label(self) -> str:
        if self.current_tzip_path:
            return os.path.basename(self.current_tzip_path)
        return "*этот файл*"

    def clear_editor(self):
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", "ziptext *этот файл*\n")
        self.current_tzip_path = None
        self._apply_syntax_highlight()
        self.status_var.set("Создан чистый документ: ziptext *этот файл*")

    def insert_setpack(self):
        snippet = '\nSetpack(my_set).storage[\n\n]\n'
        self.editor.insert(tk.INSERT, snippet)
        self._apply_syntax_highlight()
        self.status_var.set("Вставлен пустой блок Setpack(*название*).storage[*данные*].")

    def save_tzip(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить .tzip архив",
            defaultextension=".tzip",
            filetypes=[("TZIP Text Archive", "*.tzip")]
        )
        if not path:
            return
        try:
            base_dir = os.path.dirname(os.path.abspath(path))
            actual_file_name = os.path.basename(path)
            raw = self.editor.get("1.0", tk.END).strip()
            raw = sync_all_getset_files_in_text(raw, base_dir=base_dir)
            raw = resolve_get_subzips_in_text(raw, base_dir=base_dir)
            lines = raw.splitlines()
            if lines and lines[0].startswith("ziptext "):
                lines[0] = f"ziptext {actual_file_name}"
            else:
                lines.insert(0, f"ziptext {actual_file_name}")
            final_text = "\n".join(lines) + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(final_text)
            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", final_text)
            self.current_tzip_path = path
            self._apply_syntax_highlight()
            self.status_var.set(f"Сохранено: {path} (заголовок: ziptext {actual_file_name})")
        except Exception as e:
            messagebox.showerror("Ошибка сохранения .tzip", str(e))

    def open_tzip(self):
        path = filedialog.askopenfilename(title="Открыть .tzip архив", filetypes=[("TZIP Text Archive", "*.tzip")])
        if not path:
            return
        actual_file_name = os.path.basename(path)
        content = read_text_smart(path)
        content = ensure_ziptext_header(content, current_file_name=actual_file_name)
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", content)
        self.current_tzip_path = path
        self._apply_syntax_highlight()
        self.status_var.set(f"Открыт архив: {path} (заголовок: ziptext {actual_file_name})")

    def _ensure_saved_temp(self) -> str:
        raw_content = self.editor.get("1.0", tk.END)
        if self.current_tzip_path and os.path.isfile(self.current_tzip_path):
            actual_file_name = os.path.basename(self.current_tzip_path)
            raw_content = ensure_ziptext_header(raw_content, current_file_name=actual_file_name)
            with open(self.current_tzip_path, "w", encoding="utf-8") as f:
                f.write(raw_content)
            return self.current_tzip_path
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, "tzip_studio_preview.tzip")
        raw_content = ensure_ziptext_header(raw_content, current_file_name="tzip_studio_preview.tzip")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(raw_content)
        return temp_path

    def convert_zip_to_tzip_ui(self):
        zip_path = filedialog.askopenfilename(
            title="1. Выберите существующий .zip архив",
            filetypes=[("ZIP Archive", "*.zip")]
        )
        if not zip_path:
            return
        default_tzip_name = os.path.splitext(os.path.basename(zip_path))[0] + ".tzip"
        out_tzip_path = filedialog.asksaveasfilename(
            title="2. Куда сохранить новый .tzip файл",
            initialdir=os.path.dirname(zip_path),
            initialfile=default_tzip_name,
            defaultextension=".tzip",
            filetypes=[("TZIP Text Archive", "*.tzip")]
        )
        if not out_tzip_path:
            return
        try:
            saved_path, tzip_text, items = convert_existing_zip_into_tzip_file(zip_path, out_tzip_path)
            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", tzip_text)
            self.current_tzip_path = saved_path
            self._apply_syntax_highlight()
            messagebox.showinfo(
                "Готово: ZIP превращён в TZIP",
                f"Из архива '{os.path.basename(zip_path)}' создан файл:\n{saved_path}\n"
                f"Заголовок файла: ziptext {os.path.basename(saved_path)}\n\n"
                f"Обработано элементов ({len(items)}):\n" + "\n".join(items[:15])
            )
            self.status_var.set(f"Создан .tzip из ZIP: {saved_path}")
        except Exception as e:
            messagebox.showerror("Ошибка преобразования ZIP -> TZIP", str(e))

    def add_set_file_ui(self):
        file_paths = filedialog.askopenfilenames(
            title="Выберите один или несколько файлов (.txt, .cfg, .html, .js, .json, .dat)",
            filetypes=[
                ("Поддерживаемые сет-файлы", "*.txt *.cfg *.html *.js *.json *.dat"),
                ("Все файлы", "*.*")
            ]
        )
        if not file_paths:
            return
        try:
            raw_text = self.editor.get("1.0", tk.END)
            added_sets = []
            for fp in file_paths:
                raw_text, created_set, rel_p = add_single_file_as_set_to_tzip(raw_text, fp, self.current_tzip_path)
                added_sets.append(f"{rel_p} -> Setpack({created_set})")

            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", raw_text)
            self._apply_syntax_highlight()
            if self.current_tzip_path and os.path.isfile(self.current_tzip_path):
                with open(self.current_tzip_path, "w", encoding="utf-8") as f:
                    f.write(raw_text)
            messagebox.showinfo(
                "Файлы добавлены в Setpack",
                f"Успешно добавлено файлов ({len(added_sets)}):\n" + "\n".join(added_sets)
            )
            self.status_var.set(f"Добавлено сетов: {len(added_sets)}.")
        except Exception as e:
            messagebox.showerror("Ошибка добавления файла", str(e))

    def import_folder_ui(self):
        """Открывает диалог выбора строго папки и импортирует из неё все поддерживаемые файлы с сохранением пути."""
        folder_path = filedialog.askdirectory(
            title="Импорт папки — выберите папку (например, papka)",
            mustexist=True
        )
        if not folder_path or not os.path.isdir(folder_path):
            return
        try:
            raw_text = self.editor.get("1.0", tk.END)
            abs_folder = os.path.abspath(folder_path)
            folder_name = os.path.basename(abs_folder)
            added_sets = []

            for root_dir, dirs, files in os.walk(abs_folder):
                dirs.sort()
                for fname in sorted(files):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in SET_SUPPORTED_EXTS:
                        continue
                    full_fp = os.path.join(root_dir, fname)
                    rel_inside = os.path.relpath(full_fp, abs_folder).replace("\\", "/")
                    custom_rel = f"{folder_name}/{rel_inside}" if folder_name else rel_inside
                    raw_text, created_set, rel_p = add_single_file_as_set_to_tzip(
                        raw_text,
                        full_fp,
                        self.current_tzip_path,
                        custom_rel_path=custom_rel
                    )
                    added_sets.append(f"{rel_p} -> Setpack({created_set})")

            if not added_sets:
                messagebox.showwarning(
                    "Подходящих файлов не найдено",
                    f"В папке '{folder_name}' нет файлов с расширениями: {', '.join(sorted(SET_SUPPORTED_EXTS))}"
                )
                return

            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", raw_text)
            self._apply_syntax_highlight()
            if self.current_tzip_path and os.path.isfile(self.current_tzip_path):
                with open(self.current_tzip_path, "w", encoding="utf-8") as f:
                    f.write(raw_text)
            messagebox.showinfo(
                "Импорт папки завершён",
                f"Из папки '{folder_name}' импортировано файлов ({len(added_sets)}):\n" + "\n".join(added_sets[:20])
            )
            self.status_var.set(f"Импортирована папка '{folder_name}': {len(added_sets)} файл(ов).")
        except Exception as e:
            messagebox.showerror("Ошибка импорта папки", str(e))

    def import_zip_ui(self):
        zip_path = filedialog.askopenfilename(
            title="Выберите .zip архив для добавления в текущий .tzip",
            filetypes=[("ZIP Archive", "*.zip")]
        )
        if not zip_path:
            return
        try:
            raw_text = self.editor.get("1.0", tk.END)
            cur_label = self._get_current_tzip_label()
            updated_text, inside_files = pack_external_zip_to_tzip(raw_text, zip_path, current_file_name=cur_label)
            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", updated_text)
            self._apply_syntax_highlight()
            if self.current_tzip_path and os.path.isfile(self.current_tzip_path):
                with open(self.current_tzip_path, "w", encoding="utf-8") as f:
                    f.write(updated_text)
            messagebox.showinfo(
                "ZIP добавлен",
                f"Архив '{os.path.basename(zip_path)}' добавлен в текущий .tzip!\n"
                f"Файлы (.txt, .cfg, .html, .js, .json, .dat) записаны в Setpack().storage[],\n"
                f"сам архив вшит в Subzip.data {{}} ({len(inside_files)} файл(ов))."
            )
            self.status_var.set(f"Архив {os.path.basename(zip_path)} добавлен (сеты + Subzip.data).")
        except Exception as e:
            messagebox.showerror("Ошибка добавления ZIP", str(e))

    def export_subzip_ui(self):
        raw_text = self.editor.get("1.0", tk.END)
        base_dir = os.path.dirname(os.path.abspath(self.current_tzip_path)) if self.current_tzip_path else os.getcwd()
        out_zip = filedialog.asksaveasfilename(
            title="Сохранить встроенный Subzip.data как .zip",
            defaultextension=".zip",
            filetypes=[("ZIP Archive", "*.zip")]
        )
        if not out_zip:
            return
        try:
            names = extract_subzip_to_file(raw_text, out_zip, base_dir=base_dir, index=0)
            messagebox.showinfo(
                "Выгрузка Subzip.data -> .zip",
                f"Упакованный ZIP восстановлен в:\n{out_zip}\n\nСодержимое ({len(names)}):\n" + "\n".join(names[:15])
            )
            self.status_var.set(f"Subzip.data сохранён как: {out_zip}")
        except Exception as e:
            messagebox.showerror("Ошибка выгрузки Subzip.data", str(e))

    def extract_copies_ui(self):
        raw_content = self.editor.get("1.0", tk.END)
        if not raw_content.strip():
            messagebox.showwarning("Пустой редактор", "В окне редактора нет данных для распаковки!")
            return
        target_dir = filedialog.askdirectory(title="Выберите папку для распаковки .tzip (файлы, Setpack и Subzip)")
        if not target_dir:
            return
        base_dir = os.path.dirname(os.path.abspath(self.current_tzip_path)) if self.current_tzip_path else os.getcwd()
        try:
            synced_text = sync_all_getset_files_in_text(raw_content, base_dir=base_dir)
            if synced_text != raw_content:
                self.editor.delete("1.0", tk.END)
                self.editor.insert("1.0", synced_text)
                self._apply_syntax_highlight()
            files = extract_tzip_content_to_dir(synced_text, target_dir, base_dir=base_dir)
            if not files:
                messagebox.showwarning(
                    "Ничего не найдено",
                    "В текущем .tzip не найдено ни одного блока File(...), Setpack(...) или Subzip.data {}."
                )
                return
            preview_list = "\n".join(files[:20])
            if len(files) > 20:
                preview_list += f"\n... и ещё {len(files) - 20} файл(ов)"
            messagebox.showinfo("Распаковка .tzip завершена", f"Извлечено объектов ({len(files)}):\n{preview_list}")
            self.status_var.set(f"Успешно выгружено в: {target_dir} ({len(files)} объектов)")
        except Exception as e:
            messagebox.showerror("Ошибка распаковки", str(e))

    def unload_self_ui(self):
        tzip_path = self._ensure_saved_temp()
        dest_dir = filedialog.askdirectory(title="Выберите папку для выгрузки самого файла .tzip (Python)")
        if not dest_dir:
            return
        try:
            res = unload_self_tzip(tzip_path, dest_dir, move_file=False)
            messagebox.showinfo("Выгрузка себя [Python]", f"Файл .tzip успешно выгружен в:\n{res}")
            self.status_var.set(f"Архив выгружен в: {res}")
        except Exception as e:
            messagebox.showerror("Ошибка выгрузки", str(e))


def main():
    try:
        root = tk.Tk()
        app = TzipStudio1SApp(root)
        root.mainloop()
    except Exception:
        err_text = traceback.format_exc()
        log_path = os.path.join(tempfile.gettempdir(), "tzip_studio_crash.log")
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(err_text)
        try:
            err_root = tk.Tk()
            err_root.withdraw()
            messagebox.showerror("Ошибка запуска TZIPStudio(1S)", f"Не удалось запустить программу:\n\n{err_text}")
        except Exception:
            print(err_text)


if __name__ == "__main__":
    main()
