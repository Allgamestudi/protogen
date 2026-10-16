#!/usr/bin/env python3
"""GUI: пакетная конвертация SVG<->PNG (SVG->64x32 PNG или PNG->SVG)."""
import io
import re
import threading
from pathlib import Path
from tkinter import Tk, StringVar, filedialog, messagebox, END
from tkinter import ttk

import resvg_py
import vtracer
from PIL import Image


MODE_SVG_TO_PNG = "SVG → PNG (64x32)"
MODE_PNG_TO_SVG = "PNG → SVG (трассировка)"

_NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"
_RE_SVG_TAG = re.compile(r"<svg\b([^>]*)>", re.IGNORECASE | re.DOTALL)


def _normalize_svg_size(svg_text: str, target_w: int = 256, target_h: int = 128) -> str:
    m = _RE_SVG_TAG.search(svg_text)
    if not m:
        return svg_text
    attrs = m.group(1)

    def has_px_attr(name: str) -> bool:
        am = re.search(rf'\b{name}\s*=\s*"([^"]+)"', attrs, re.IGNORECASE)
        if not am:
            return False
        return bool(re.fullmatch(rf"{_NUM}(?:px)?", am.group(1).strip()))

    if has_px_attr("width") and has_px_attr("height"):
        return svg_text

    w, h = target_w, target_h
    vb = re.search(r'\bviewBox\s*=\s*"\s*([^"]+)"', attrs, re.IGNORECASE)
    if vb:
        parts = re.split(r"[\s,]+", vb.group(1).strip())
        if len(parts) == 4:
            try:
                vw, vh = float(parts[2]), float(parts[3])
                if vw > 0 and vh > 0:
                    w, h = int(round(vw)), int(round(vh))
            except ValueError:
                pass

    new_attrs = re.sub(r'\s*(?:width|height)\s*=\s*"[^"]*"', "", attrs, flags=re.IGNORECASE)
    new_tag = f'<svg{new_attrs} width="{w}" height="{h}">'
    return svg_text[:m.start()] + new_tag + svg_text[m.end():]


def svg_to_64x32(src: Path, dst: Path, bg: str | None = None) -> None:
    svg_text = _normalize_svg_size(src.read_text(encoding="utf-8"))
    raw = resvg_py.svg_to_bytes(svg_string=svg_text)
    png_bytes = bytes(raw) if isinstance(raw, list) else raw

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    img = img.resize((64, 32), Image.LANCZOS)

    if bg:
        flat = Image.new("RGBA", img.size, bg)
        flat.alpha_composite(img)
        img = flat.convert("RGB")
    img.save(dst, optimize=True)


def png_to_svg(src: Path, dst: Path, colormode: str = "color") -> None:
    """Векторизация растра. colormode: 'color' или 'binary'."""
    vtracer.convert_image_to_svg_py(
        str(src),
        str(dst),
        colormode=colormode,
        hierarchical="stacked",
        mode="spline",
        filter_speckle=4,
        color_precision=6,
        layer_difference=16,
        corner_threshold=60,
        length_threshold=4.0,
        max_iterations=10,
        splice_threshold=45,
        path_precision=3,
    )


class App:
    def __init__(self, root: Tk) -> None:
        root.title("SVG ↔ PNG")
        root.geometry("680x500")
        root.minsize(560, 400)

        self.files: list[Path] = []
        self.export_dir = StringVar(value="")
        self.bg = StringVar(value="")
        self.mode = StringVar(value=MODE_SVG_TO_PNG)
        self.colormode = StringVar(value="color")
        self.status = StringVar(value="Готов.")

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # Mode selector
        mode_frame = ttk.Frame(main)
        mode_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(mode_frame, text="Режим:").pack(side="left")
        mode_cb = ttk.Combobox(
            mode_frame, textvariable=self.mode,
            values=[MODE_SVG_TO_PNG, MODE_PNG_TO_SVG],
            state="readonly", width=28,
        )
        mode_cb.pack(side="left", padx=6)
        mode_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())

        # Top bar: import / remove / clear
        top = ttk.Frame(main)
        top.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.import_btn = ttk.Button(top, text="Импорт SVG…", command=self.import_files)
        self.import_btn.pack(side="left")
        ttk.Button(top, text="Убрать выбранное", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(top, text="Очистить", command=self.clear_files).pack(side="left")

        # File list
        list_frame = ttk.Frame(main)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.listbox = ttk.Treeview(list_frame, columns=("path",), show="headings", selectmode="extended")
        self.listbox.heading("path", text="Файл")
        self.listbox.column("path", anchor="w")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)

        # Export path
        exp = ttk.LabelFrame(main, text="Папка экспорта", padding=8)
        exp.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        exp.columnconfigure(0, weight=1)
        ttk.Entry(exp, textvariable=self.export_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(exp, text="Выбрать…", command=self.choose_export).grid(row=0, column=1, padx=(6, 0))

        # Options (mode-specific)
        self.opts_frame = ttk.LabelFrame(main, text="Опции", padding=8)
        self.opts_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self.bg_label = ttk.Label(self.opts_frame, text="Фон (hex, пусто = прозрачный):")
        self.bg_entry = ttk.Entry(self.opts_frame, textvariable=self.bg, width=12)

        self.color_label = ttk.Label(self.opts_frame, text="Цветовой режим:")
        self.color_cb = ttk.Combobox(
            self.opts_frame, textvariable=self.colormode,
            values=["color", "binary"], state="readonly", width=10,
        )

        # Convert + progress
        bottom = ttk.Frame(main)
        bottom.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(1, weight=1)
        self.convert_btn = ttk.Button(bottom, text="Конвертировать", command=self.start_convert)
        self.convert_btn.grid(row=0, column=0)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Label(main, textvariable=self.status, anchor="w").grid(row=6, column=0, sticky="ew", pady=(8, 0))

        self._on_mode_change()

    def _on_mode_change(self) -> None:
        # перерисовываем опции под текущий режим и обновляем подписи кнопок
        for child in self.opts_frame.winfo_children():
            child.pack_forget()

        if self.mode.get() == MODE_SVG_TO_PNG:
            self.import_btn.configure(text="Импорт SVG…")
            self.bg_label.pack(side="left")
            self.bg_entry.pack(side="left", padx=6)
        else:
            self.import_btn.configure(text="Импорт PNG…")
            self.color_label.pack(side="left")
            self.color_cb.pack(side="left", padx=6)

    def _current_input_filter(self) -> tuple[str, list[tuple[str, str]]]:
        if self.mode.get() == MODE_SVG_TO_PNG:
            return ("Выбери SVG", [("SVG", "*.svg"), ("Все файлы", "*.*")])
        return ("Выбери PNG", [("PNG / JPG", "*.png *.jpg *.jpeg *.bmp"), ("Все файлы", "*.*")])

    def import_files(self) -> None:
        title, types = self._current_input_filter()
        paths = filedialog.askopenfilenames(title=title, filetypes=types)
        added = 0
        for p in paths:
            path = Path(p)
            if path not in self.files:
                self.files.append(path)
                self.listbox.insert("", END, values=(str(path),))
                added += 1
        if added:
            self.status.set(f"Добавлено: {added}. Всего: {len(self.files)}.")

    def remove_selected(self) -> None:
        sel = self.listbox.selection()
        if not sel:
            return
        to_remove = {Path(self.listbox.item(i, "values")[0]) for i in sel}
        self.files = [f for f in self.files if f not in to_remove]
        for i in sel:
            self.listbox.delete(i)
        self.status.set(f"Осталось: {len(self.files)}.")

    def clear_files(self) -> None:
        self.files.clear()
        for i in self.listbox.get_children():
            self.listbox.delete(i)
        self.status.set("Список очищен.")

    def choose_export(self) -> None:
        d = filedialog.askdirectory(title="Папка экспорта")
        if d:
            self.export_dir.set(d)

    def start_convert(self) -> None:
        if not self.files:
            messagebox.showwarning("Пусто", "Сначала импортируй файлы.")
            return
        if not self.export_dir.get():
            messagebox.showwarning("Нет папки", "Укажи папку экспорта.")
            return
        out_dir = Path(self.export_dir.get())
        out_dir.mkdir(parents=True, exist_ok=True)

        mode = self.mode.get()
        bg = self.bg.get().strip() or None
        colormode = self.colormode.get()

        self.convert_btn.configure(state="disabled")
        self.progress.configure(maximum=len(self.files), value=0)

        threading.Thread(
            target=self._convert_worker,
            args=(out_dir, mode, bg, colormode),
            daemon=True,
        ).start()

    def _convert_worker(self, out_dir: Path, mode: str, bg: str | None, colormode: str) -> None:
        ok, fail = 0, 0
        errors: list[str] = []
        for i, src in enumerate(self.files, 1):
            try:
                if mode == MODE_SVG_TO_PNG:
                    dst = out_dir / (src.stem + ".png")
                    svg_to_64x32(src, dst, bg)
                else:
                    dst = out_dir / (src.stem + ".svg")
                    png_to_svg(src, dst, colormode)
                ok += 1
            except Exception as e:
                fail += 1
                errors.append(f"{src.name}: {e}")
            self.progress.after(0, self._update_progress, i, src.name)

        self.progress.after(0, self._finish, ok, fail, errors)

    def _update_progress(self, value: int, name: str) -> None:
        self.progress.configure(value=value)
        self.status.set(f"[{value}/{len(self.files)}] {name}")

    def _finish(self, ok: int, fail: int, errors: list[str]) -> None:
        self.convert_btn.configure(state="normal")
        self.status.set(f"Готово. Успешно: {ok}, ошибок: {fail}.")
        if errors:
            messagebox.showerror("Ошибки", "\n".join(errors[:20]))
        else:
            messagebox.showinfo("Готово", f"Сконвертировано: {ok}")


def main() -> None:
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
