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
    """Экранирует закрывающую квадратную скобку, чтобы не сломать блок .storage[...]."""
    return text.replace("\\", "\\\\").replace("]", "\\]")


def unescape_storage_data(text: str) -> str:
    """Восстанавливает исходные символы из блока .storage[...]."""
    return text.replace("\\]", "]").replace("\\\\", "\\")


def parse_setpacks_from_body(body: str) -> dict:
    """Надёжно парсит блоки Setpack(имя).storage[данные] с поддержкой экранированных скобок."""
    setpacks = {}
    pattern = re.compile(r'Setpack\(\s*([^)]+?)\s*\)\.storage\[((?:\\.|[^\\\]])*)\]', re.DOTALL)
    for sp_match in pattern.finditer(body):
        sp_name = sp_match.group(1).strip().strip('"').strip("'")
        sp_data = sp_match.group(2)
        if sp_data.startswith("\r\n"):
            sp_data = sp_data[2:]
        elif sp_data.startswith("\n"):
            sp_data = sp_data[1:]
        if sp_data.endswith("\r\n"):
            sp_data = sp_data[:-2]
        elif sp_data.endswith("\n"):
            sp_data = sp_data[:-1]
        setpacks[sp_name] = unescape_storage_data(sp_data)
    return setpacks


def sync_all_getset_files_in_text(tzip_content: str, base_dir: str = ".") -> str:
    """
    Находит все конструкции File( ..., get[путь].getset("имя_сета") ) в тексте .tzip.
    Если файл по пути существует, автоматически считывает его содержимое и записывает
    в соответствующий блок Setpack(имя_сета).storage[ ... ] (создавая или обновляя его).
    """
    getset_pattern = re.compile(r'get\[([^\]]+)\]\.getset\(\s*["\']([^"\']+)["\']\s*\)')
    matches = list(getset_pattern.finditer(tzip_content))
    if not matches:
        return tzip_content

    updated_text = tzip_content
    for m in matches:
        rel_path = m.group(1).strip()
        set_name = m.group(2).strip()
        full_src = rel_path if os.path.isabs(rel_path) else os.path.join(base_dir, rel_path)
        if not os.path.isfile(full_src):
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
        get_zip_match = re.fullmatch(r'get\[([^\]]+)\]', raw_inner)
        if get_zip_match:
            zip_rel_path = get_zip_match.group(1).strip()
            full_zip_path = zip_rel_path if os.path.isabs(zip_rel_path) else os.path.join(base_dir, zip_rel_path)
            if os.path.isfile(full_zip_path):
                with open(full_zip_path, "rb") as zf_file:
                    b64_str = base64.b64encode(zf_file.read()).decode("ascii")
                subzips.append({
                    "source": os.path.basename(zip_rel_path),
                    "b64": b64_str
                })
        else:
            cleaned_b64 = "".join(raw_inner.split())
            if cleaned_b64:
                subzips.append({
                    "source": "embedded",
                    "b64": cleaned_b64
                })

    body_without_subzip = subzip_pattern.sub("", body)

    # 1. Парсинг Setpack(*название*).storage[*данные*]
    setpacks = parse_setpacks_from_body(body_without_subzip)
    sp_remove_pattern = re.compile(r'Setpack\(\s*[^)]+?\s*\)\.storage\[(?:\\.|[^\\\]])*\]', re.DOTALL)
    body_for_files = sp_remove_pattern.sub("", body_without_subzip)

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
            
        arch_match = re.search(r'archive\[([^\]]*)\]', args_raw)
        archive_subpath = arch_match.group(1).strip().strip("/\\") if arch_match else ""
        
        get_set_match = re.search(r'get\[([^\]]+)\]\.getset\(\s*["\']([^"\']+)["\']\s*\)', args_raw)
        get_data_match = re.search(r'get\[([^\]]+)\]\.getdata\(\)', args_raw)
        get_plain_match = re.search(r'get\[([^\]]+)\]', args_raw)
        
        set_name = None
        get_source_path = None
        mode = "inline"
        
        if get_set_match:
            get_source_path = get_set_match.group(1).strip()
            set_name = get_set_match.group(2).strip()
            mode = "getset"
        elif get_data_match:
            get_source_path = get_data_match.group(1).strip()
            mode = "getdata"
        elif get_plain_match:
            get_source_path = get_plain_match.group(1).strip()
            mode = "get_plain"
            
        filename = None
        if get_source_path:
            filename = os.path.basename(get_source_path)
        elif set_name:
            filename = set_name if "." in set_name else f"{set_name}.txt"
        else:
            cleaned = re.sub(r'archive\[[^\]]*\]', '', args_raw).strip(" ,")
            str_match = re.search(r'["\']([^"\']+)["\']', cleaned)
            if str_match:
                filename = str_match.group(1)
        if not filename:
            filename = f"file_{len(entries) + 1}.txt"
            
        resolved_text = ""
        if inline_body is not None:
            resolved_text = inline_body.strip("\r\n")
        elif get_source_path:
            full_src = get_source_path if os.path.isabs(get_source_path) else os.path.join(base_dir, get_source_path)
            if os.path.isfile(full_src):
                resolved_text = read_text_smart(full_src)
                
        if set_name and set_name in setpacks:
            sp_content = setpacks[set_name]
            if sp_content:
                resolved_text = sp_content
        elif not resolved_text and get_source_path and mode != "getset":
            resolved_text = f"[Источник не найден: {get_source_path}]"
                
        entries.append({
            "archive_subpath": archive_subpath,
            "get_source_path": get_source_path or "",
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
            arc_path = info.filename.replace("\\", "/")
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


def add_single_file_as_set_to_tzip(tzip_content: str, file_path: str, tzip_path: str = None) -> tuple:
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
    try:
        rel_full = os.path.relpath(abs_file, tzip_dir).replace("\\", "/")
    except ValueError:
        rel_full = base_name

    if rel_full.startswith(".."):
        archive_dir = ""
        get_path = base_name
    else:
        archive_dir = os.path.dirname(rel_full)
        get_path = rel_full

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
    return updated_tzip, set_name


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
            arc_path = info.filename.replace("\\", "/")
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
    subzip_pattern = re.compile(r'Subzip\.data\s*\{\s*get\[([^\]]+)\]\s*\}', re.DOTALL)

    def _replacer(match):
        zip_rel_path = match.group(1).strip()
        full_zip_path = zip_rel_path if os.path.isabs(zip_rel_path) else os.path.join(base_dir, zip_rel_path)
        if not os.path.isfile(full_zip_path):
            raise FileNotFoundError(f"Архив для Subzip.data не найден: {full_zip_path}")
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
        subpath = item["archive_subpath"].replace("/", os.sep).replace("\\", os.sep)
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


class TzipStudioAlphaApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("TZIPStudio(alpha)")
        self.root.geometry("1080x720")
        self.root.configure(bg="#1e1e2e")
        self.current_tzip_path = None
        self._build_ui()

    def _build_ui(self):
        header = tk.Label(
            self.root,
            text="TZIPStudio(alpha) — Setpack(), .getset(), ZIP -> TZIP & Subzip.data {}",
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

        self.status_var = tk.StringVar(value="TZIPStudio(alpha) готов к работе.")
        status_bar = tk.Label(self.root, textvariable=self.status_var, bg="#11111b", fg="#a6adc8", anchor="w", padx=12, pady=5, font=("Segoe UI", 9))
        status_bar.pack(fill="x", side="bottom")

        self.clear_editor()

    def _get_current_tzip_label(self) -> str:
        if self.current_tzip_path:
            return os.path.basename(self.current_tzip_path)
        return "*этот файл*"

    def clear_editor(self):
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", "ziptext *этот файл*\n")
        self.current_tzip_path = None
        self.status_var.set("Создан чистый документ: ziptext *этот файл*")

    def insert_setpack(self):
        snippet = '\nSetpack(my_set).storage[\n\n]\n'
        self.editor.insert(tk.INSERT, snippet)
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
                raw_text, created_set = add_single_file_as_set_to_tzip(raw_text, fp, self.current_tzip_path)
                added_sets.append(f"{os.path.basename(fp)} -> Setpack({created_set})")

            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", raw_text)
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
        app = TzipStudioAlphaApp(root)
        root.mainloop()
    except Exception:
        err_text = traceback.format_exc()
        log_path = os.path.join(tempfile.gettempdir(), "tzip_studio_crash.log")
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(err_text)
        try:
            err_root = tk.Tk()
            err_root.withdraw()
            messagebox.showerror("Ошибка запуска TZIPStudio(alpha)", f"Не удалось запустить программу:\n\n{err_text}")
        except Exception:
            print(err_text)


if __name__ == "__main__":
    main()
