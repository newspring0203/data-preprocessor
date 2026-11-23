"""Mac desktop interface for one local tax filing at a time."""
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from extract import OUTPUT_COLUMNS, ROOT, collect, output_row, save_output
from scan_ocr import ScanOCR
from table_model import TableModel

MODELS = ROOT / "models"
NAVY = "#163247"
MUTED = "#495e6e"
BACKGROUND = "#eef2f5"


class Desktop:
    def __init__(self, window):
        self.window = window
        self.filing = None
        self.statements = {}
        self.rows = []
        self.messages = Queue()
        self.model = None
        self.ocr = ScanOCR(MODELS / "ppocr")

        window.title("Data Preprocessor")
        window.geometry("960x660")
        window.minsize(740, 560)
        window.configure(background=BACKGROUND)
        style = ttk.Style(window)
        style.configure("Workspace.TFrame", background=BACKGROUND)
        style.configure("Heading.TLabel", background=BACKGROUND, foreground=NAVY,
                        font=("Apple SD Gothic Neo", 23, "bold"))
        style.configure("Status.TLabel", background=BACKGROUND, foreground=MUTED,
                        font=("Apple SD Gothic Neo", 13))
        style.configure("Treeview", rowheight=29, font=("Apple SD Gothic Neo", 13))
        style.configure("Treeview.Heading", font=("Apple SD Gothic Neo", 13, "bold"))

        panel = ttk.Frame(window, padding=24, style="Workspace.TFrame")
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(6, weight=1)
        ttk.Label(panel, text="Data Preprocessor", style="Heading.TLabel").grid(sticky="w")
        self.filing_label = tk.StringVar(value="신고서 PDF를 선택하세요.")
        self.statement_labels = {kind: tk.StringVar(value="선택 사항")
                                 for kind in ("재무상태표", "손익계산서")}
        self.status = tk.StringVar(value="PDF 선택 대기 중")
        self.filing_button = self.file_row(panel, 1, "신고서 선택", self.choose_filing, self.filing_label)
        self.statement_buttons = [
            self.file_row(panel, row, f"{kind} 선택", lambda kind=kind: self.choose_statement(kind), label)
            for row, (kind, label) in enumerate(self.statement_labels.items(), 2)
        ]
        actions = ttk.Frame(panel, style="Workspace.TFrame")
        actions.grid(row=4, sticky="ew", pady=(14, 12))
        self.run_button = ttk.Button(actions, text="추출 시작", command=self.run, state="disabled")
        self.run_button.pack(side="left")
        self.save_button = ttk.Button(actions, text="엑셀 저장", command=self.save, state="disabled")
        self.save_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(actions, length=116, mode="indeterminate")
        self.progress.pack(side="right")
        ttk.Label(panel, textvariable=self.status, style="Status.TLabel").grid(row=5, sticky="w", pady=(0, 6))
        table_panel = ttk.Frame(panel)
        table_panel.grid(row=6, sticky="nsew")
        table_panel.rowconfigure(0, weight=1)
        table_panel.columnconfigure(0, weight=1)
        columns = OUTPUT_COLUMNS
        self.table = ttk.Treeview(table_panel, columns=columns, show="headings")
        for column, width in zip(columns, (440, 180, 120), strict=True):
            self.table.heading(column, text=column)
            self.table.column(column, width=width, minwidth=78,
                              anchor="e" if column == "금액(원)" else "w")
        self.table.tag_configure("alternate", background="#f1f5f8")
        bar = ttk.Scrollbar(table_panel, command=self.table.yview)
        self.table.configure(yscrollcommand=bar.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")

        self.warnings = tk.Text(panel, wrap="word", height=4, state="disabled",
                                relief="flat", padx=10, pady=7, background="#ffffff",
                                foreground=NAVY, font=("Apple SD Gothic Neo", 13))
        self.warnings.grid(row=7, sticky="ew", pady=(10, 0))

    @staticmethod
    def file_row(parent, row, title, command, variable):
        line = ttk.Frame(parent, style="Workspace.TFrame")
        line.grid(row=row, sticky="ew", pady=(7, 0))
        button = ttk.Button(line, text=title, command=command, width=14)
        button.pack(side="left")
        ttk.Label(line, textvariable=variable, style="Status.TLabel",
                  wraplength=590).pack(side="left", padx=(10, 0))
        return button

    def choose_filing(self):
        filename = filedialog.askopenfilename(parent=self.window, filetypes=[("PDF 파일", "*.pdf")])
        if filename:
            self.filing = Path(filename)
            if self.filing in self.statements.values():
                for kind, path in tuple(self.statements.items()):
                    if path == self.filing:
                        del self.statements[kind]
                        self.statement_labels[kind].set("선택 사항")
            self.filing_label.set(self.filing.name)
            self.clear()

    def choose_statement(self, kind):
        filename = filedialog.askopenfilename(parent=self.window, filetypes=[("PDF 파일", "*.pdf")])
        if not filename:
            return
        path = Path(filename)
        if path == self.filing or any(path == item for other, item in self.statements.items() if other != kind):
            messagebox.showerror("중복 파일", "다른 입력 칸에서 이미 선택한 파일입니다.", parent=self.window)
            return
        self.statements[kind] = path
        self.statement_labels[kind].set(path.name)
        self.clear()

    def clear(self):
        self.rows = []
        self.table.delete(*self.table.get_children())
        self.set_warnings([])
        self.status.set("추출 준비됨" if self.filing else "PDF 선택 대기 중")
        self.run_button.configure(state="normal" if self.filing else "disabled")
        self.save_button.configure(state="disabled")

    def set_warnings(self, warnings):
        self.warnings.configure(state="normal")
        self.warnings.delete("1.0", "end")
        if warnings:
            self.warnings.insert("1.0", "\n".join(f"확인 필요 — {line}" for line in warnings))
        self.warnings.configure(state="disabled")

    def run(self):
        self.clear()
        paths = [self.filing, *self.statements.values()]
        if paths[0] is None:
            return
        self.begin("PDF에서 지정 항목을 읽는 중…")
        self.start_job("extract", lambda: self.extract(paths))

    def extract(self, paths):
        if self.model is None:
            self.model = TableModel(MODELS)
        return collect(paths, self.ocr, self.model)

    def save(self):
        if not self.rows:
            return
        filename = filedialog.asksaveasfilename(
            parent=self.window, defaultextension=".xlsx", filetypes=[("Excel 통합 문서", "*.xlsx")],
            initialfile=f"{self.filing.stem} 추출 결과.xlsx",
        )
        if filename:
            target = Path(filename)
            rows = tuple(self.rows)
            self.begin("엑셀 파일을 저장하는 중…")
            self.start_job("save", lambda: self.write(rows, target))

    @staticmethod
    def write(rows, target):
        save_output(rows, target)
        return target

    def begin(self, status):
        self.status.set(status)
        for button in (self.filing_button, *self.statement_buttons, self.run_button, self.save_button):
            button.configure(state="disabled")
        self.progress.start(12)

    def start_job(self, name, task):
        def worker():
            try:
                self.messages.put((name, task(), None))
            except Exception as error:
                self.messages.put((name, None, str(error)))
        Thread(target=worker, daemon=True).start()
        self.window.after(100, self.finish_job)

    def finish_job(self):
        try:
            name, result, error = self.messages.get_nowait()
        except Empty:
            self.window.after(100, self.finish_job)
            return
        self.progress.stop()
        for button in (self.filing_button, *self.statement_buttons, self.run_button):
            button.configure(state="normal")
        if error is not None:
            self.status.set("처리 실패")
            messagebox.showerror("처리 실패", error, parent=self.window)
        elif name == "save":
            self.status.set(f"저장 완료 · {result.name}")
        else:
            self.rows, warnings = result
            self.set_warnings(warnings)
            for index, row in enumerate(self.rows):
                label, value, note = output_row(row)
                self.table.insert("", "end", values=(label, f"{value:,}", note or ""),
                                  tags=("alternate",) if index % 2 else ())
            self.status.set(f"{len(self.rows)}개 항목 추출 · 확인 필요 {len(warnings)}건")
        self.save_button.configure(state="normal" if self.rows else "disabled")


def main():
    window = tk.Tk()
    Desktop(window)
    window.mainloop()


if __name__ == "__main__":
    main()
