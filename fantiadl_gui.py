#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fantiadl GUI - simple tkinter frontend."""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SETTINGS_VERSION = 1
MAX_URL_HISTORY = 5


def settings_path():
    """Per-user config path. %APPDATA%\\fantiadl-gui\\settings.json on Windows,
    $XDG_CONFIG_HOME/fantiadl-gui/settings.json (or ~/.config/...) elsewhere.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "fantiadl-gui", "settings.json")

# When the executable is invoked with this sentinel as argv[1], delegate to
# the fantiadl CLI. Lets the bundled exe serve as both GUI and CLI.
CLI_SENTINEL = "__fantiadl_cli__"
if len(sys.argv) >= 2 and sys.argv[1] == CLI_SENTINEL:
    sys.argv = ["fantiadl"] + sys.argv[2:]
    from fantiadl.fantiadl import cli
    cli()
    sys.exit(0)


def child_command(args):
    """Build the argv that re-launches this binary in CLI mode."""
    if getattr(sys, "frozen", False):
        # PyInstaller-bundled: sys.executable is the bundled exe.
        return [sys.executable, CLI_SENTINEL] + args
    # Running as plain python script.
    return [sys.executable, os.path.abspath(sys.argv[0]), CLI_SENTINEL] + args


class FantiadlGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("fantiadl GUI")
        self.root.geometry("960x780")

        self.proc = None
        self.reader_thread = None
        self.log_queue = queue.Queue()
        self.last_report_path = None

        self._build_ui()
        self._load_settings()
        self.root.after(80, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        # Shared inputs
        header = ttk.LabelFrame(self.root, text="共用設定", padding=8)
        header.pack(fill=tk.X, padx=10, pady=(10, 4))

        ttk.Label(header, text="Session Cookie / cookies.txt:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.cookie_var = tk.StringVar()
        self.cookie_entry = ttk.Entry(header, textvariable=self.cookie_var, show="*")
        self.cookie_entry.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(header, text="瀏覽…", command=self._browse_cookie).grid(row=0, column=2, padx=4)
        self.show_cookie_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text="顯示", variable=self.show_cookie_var,
                        command=self._toggle_cookie).grid(row=0, column=3, padx=4)
        self.remember_cookie_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text="記住 Cookie（明文儲存到本地）",
                        variable=self.remember_cookie_var,
                        command=self._on_remember_cookie_toggled).grid(
                            row=0, column=4, padx=4, sticky="w")

        ttk.Label(header, text="輸出資料夾:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.outdir_var = tk.StringVar(value=os.getcwd())
        ttk.Entry(header, textvariable=self.outdir_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(header, text="瀏覽…", command=self._browse_outdir).grid(row=1, column=2, padx=4)

        ttk.Label(header, text="資料庫檔 (.sqlite):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self.db_var = tk.StringVar()
        ttk.Entry(header, textvariable=self.db_var).grid(row=2, column=1, sticky="ew", padx=4)
        db_btns = ttk.Frame(header)
        db_btns.grid(row=2, column=2, columnspan=2, sticky="w")
        ttk.Button(db_btns, text="開啟…", command=self._open_db, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Button(db_btns, text="建立新…", command=self._create_db, width=10).pack(side=tk.LEFT, padx=2)

        header.columnconfigure(1, weight=1)

        # Notebook with three tabs
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.X, padx=10, pady=4)

        # ---- Tab 1: Download ----
        tab_dl = ttk.Frame(nb, padding=8)
        nb.add(tab_dl, text="下載")

        ttk.Label(tab_dl, text="URL (fanclub 或 post):").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.dl_url_var = tk.StringVar()
        self.dl_url_history = []
        self.dl_url_combo = ttk.Combobox(tab_dl, textvariable=self.dl_url_var)
        self.dl_url_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4)

        ttk.Label(tab_dl, text="限制 post 數 (0=全部):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.dl_limit_var = tk.StringVar(value="0")
        ttk.Entry(tab_dl, textvariable=self.dl_limit_var, width=10).grid(row=1, column=1, sticky="w", padx=4)

        self.dl_continue_var = tk.BooleanVar(value=True)
        self.dl_metadata_var = tk.BooleanVar(value=False)
        self.dl_thumb_var = tk.BooleanVar(value=False)
        self.dl_use_server_filenames_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab_dl, text="遇錯繼續 (-i)", variable=self.dl_continue_var).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(tab_dl, text="dump metadata (-m)", variable=self.dl_metadata_var).grid(row=3, column=1, sticky="w", padx=4)
        ttk.Checkbutton(tab_dl, text="下載 thumbnail (-t)", variable=self.dl_thumb_var).grid(row=4, column=1, sticky="w", padx=4)
        ttk.Checkbutton(tab_dl, text="使用 server 檔名 (-s)", variable=self.dl_use_server_filenames_var).grid(row=5, column=1, sticky="w", padx=4)

        btns_dl = ttk.Frame(tab_dl)
        btns_dl.grid(row=6, column=1, sticky="w", pady=8)
        self.dl_btn = ttk.Button(btns_dl, text="開始下載", command=self._run_download)
        self.dl_btn.pack(side=tk.LEFT, padx=4)

        tab_dl.columnconfigure(1, weight=1)

        # ---- Tab 2: Verify ----
        tab_v = ttk.Frame(nb, padding=8)
        nb.add(tab_v, text="驗證 / 交叉比對")

        ttk.Label(tab_v, text="Fanclub URL:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.v_url_var = tk.StringVar()
        self.v_url_history = []
        self.v_url_combo = ttk.Combobox(tab_v, textvariable=self.v_url_var)
        self.v_url_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4)

        ttk.Label(tab_v, text="JSON 報告路徑\n(留空自動命名):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.v_json_var = tk.StringVar()
        ttk.Entry(tab_v, textvariable=self.v_json_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(tab_v, text="選擇…", command=self._browse_json).grid(row=1, column=2, padx=4)

        ttk.Label(tab_v, text="限制 post 數 (0=全部):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self.v_limit_var = tk.StringVar(value="0")
        ttk.Entry(tab_v, textvariable=self.v_limit_var, width=10).grid(row=2, column=1, sticky="w", padx=4)

        btns_v = ttk.Frame(tab_v)
        btns_v.grid(row=3, column=1, sticky="w", pady=8)
        ttk.Button(btns_v, text="只驗證（報告）", command=lambda: self._run_verify(auto_fix=False)).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns_v, text="驗證並補抓", command=lambda: self._run_verify(auto_fix=True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns_v, text="開啟最近報告", command=self._open_last_report).pack(side=tk.LEFT, padx=4)

        tab_v.columnconfigure(1, weight=1)

        # ---- Tab 3: Advanced ----
        tab_a = ttk.Frame(nb, padding=8)
        nb.add(tab_a, text="進階選項")

        adv_defs = [
            ("sleep-request (秒，每次請求前 sleep)",     "sleep_request",         "1.5", "預防 429 最有效的旗標。0=關閉。"),
            ("max-retries (urllib3 內層重試數)",          "max_retries",           "10",  ""),
            ("retry-backoff (退避 factor)",               "retry_backoff",         "3",   "delay = factor × 2^(n-1)"),
            ("retry-backoff-max (單次重試秒上限)",        "retry_backoff_max",     "120", ""),
            ("cooldown-seconds (外層冷卻基數秒)",         "cooldown_seconds",      "60",  "內層用完後外層 wait = base × 第 n 輪"),
            ("cooldown-attempts (外層冷卻最多幾輪)",      "cooldown_attempts",     "10",  ""),
            ("post-directory-format (資料夾命名)",        "post_directory_format", "{post_id}_{post_title}",
             "欄位: {post_id} {post_title} {post_creator} {fanclub_id}"),
            ("max-title-length (檔名截斷上限)",           "max_title_length",      "80",  "Windows MAX_PATH=260 注意。0=不截"),
        ]
        self.adv_vars = {}
        for i, (label, key, default, hint) in enumerate(adv_defs):
            ttk.Label(tab_a, text=label).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            var = tk.StringVar(value=default)
            self.adv_vars[key] = var
            ttk.Entry(tab_a, textvariable=var, width=40).grid(row=i, column=1, sticky="ew", padx=4)
            if hint:
                ttk.Label(tab_a, text=hint, foreground="#666").grid(row=i, column=2, sticky="w", padx=4)
        tab_a.columnconfigure(1, weight=1)

        # ---- Log + control bar ----
        log_frame = ttk.LabelFrame(self.root, text="輸出 log", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self.log_text = tk.Text(log_frame, wrap=tk.NONE, height=16, font=("Consolas", 9))
        log_scroll_y = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll_x = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_scroll_y.set, xscrollcommand=log_scroll_x.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll_y.grid(row=0, column=1, sticky="ns")
        log_scroll_x.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.status_var = tk.StringVar(value="閒置")
        ttk.Label(ctrl, textvariable=self.status_var, foreground="#0a5").pack(side=tk.LEFT)
        ttk.Button(ctrl, text="清空 log", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.RIGHT, padx=4)
        self.stop_btn = ttk.Button(ctrl, text="停止", command=self._stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=4)

    # --------------------------------------------------------- UI helpers

    def _toggle_cookie(self):
        self.cookie_entry.configure(show="" if self.show_cookie_var.get() else "*")

    def _browse_cookie(self):
        path = filedialog.askopenfilename(title="選擇 cookies.txt",
                                          filetypes=[("cookies.txt", "*.txt"), ("All files", "*.*")])
        if path:
            self.cookie_var.set(path)

    def _browse_outdir(self):
        path = filedialog.askdirectory(title="選擇輸出資料夾")
        if path:
            self.outdir_var.set(path)

    def _open_db(self):
        path = filedialog.askopenfilename(title="選擇現有的 SQLite 資料庫",
                                          filetypes=[("SQLite", "*.sqlite *.db *.sqlite3"),
                                                     ("All files", "*.*")])
        if path:
            self.db_var.set(path)
            self._append_log("已選擇資料庫: {}\n".format(path))

    def _create_db(self):
        outdir = self.outdir_var.get().strip() or os.getcwd()
        path = filedialog.asksaveasfilename(
            title="建立新的 SQLite 資料庫",
            defaultextension=".sqlite",
            initialdir=outdir,
            initialfile="fantia.sqlite",
            filetypes=[("SQLite", "*.sqlite"), ("All files", "*.*")],
        )
        if not path:
            return
        if os.path.exists(path):
            if not messagebox.askyesno("檔案已存在",
                                       "{} 已存在。\n要直接套用此檔（不會覆寫，現有資料保留）嗎？".format(os.path.basename(path))):
                return
            self.db_var.set(path)
            self._append_log("使用既有資料庫: {}\n".format(path))
            return
        try:
            # Eagerly create + init the schema so the user sees confirmation now,
            # not at first download. Uses fantiadl's own DB class for table parity.
            from fantiadl.db import FantiaDlDatabase
            db = FantiaDlDatabase(path)
            # Drop the connection immediately so the file isn't locked on Windows.
            if db.conn is not None:
                db.conn.close()
                db.conn = None
        except Exception as e:
            messagebox.showerror("建立失敗", "無法建立資料庫:\n{}".format(e))
            return
        self.db_var.set(path)
        self._append_log("已建立資料庫: {}\n".format(path))
        messagebox.showinfo("已建立", "新資料庫已建立並初始化:\n{}".format(path))

    def _browse_json(self):
        path = filedialog.asksaveasfilename(title="JSON 報告路徑",
                                            defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            self.v_json_var.set(path)

    def _open_last_report(self):
        path = self.last_report_path
        if not path or not os.path.isfile(path):
            messagebox.showinfo("沒有報告", "找不到最近的 JSON 報告檔。先跑一次驗證。")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            messagebox.showinfo("檔案位置", path)

    # ---------------------------------------------------- Arg construction

    def _common_args(self):
        args = []
        cookie = self.cookie_var.get().strip()
        outdir = self.outdir_var.get().strip()
        db = self.db_var.get().strip()
        if cookie:
            args += ["-c", cookie]
        if outdir:
            args += ["-o", outdir]
        if db:
            args += ["--db", db]
        adv_flag_map = {
            "sleep_request":         "--sleep-request",
            "max_retries":           "--max-retries",
            "retry_backoff":         "--retry-backoff",
            "retry_backoff_max":     "--retry-backoff-max",
            "cooldown_seconds":      "--cooldown-seconds",
            "cooldown_attempts":     "--cooldown-attempts",
            "post_directory_format": "--post-directory-format",
            "max_title_length":      "--max-title-length",
        }
        for key, flag in adv_flag_map.items():
            val = self.adv_vars[key].get().strip()
            if val == "":
                continue
            args += [flag, val]
        return args

    # ---------------------------------------------------- Run actions

    def _run_download(self):
        if self._is_running():
            messagebox.showwarning("執行中", "已有任務在跑，請先停止。")
            return
        url = self.dl_url_var.get().strip()
        if not url:
            messagebox.showerror("缺欄位", "請輸入下載 URL。")
            return
        if not self.cookie_var.get().strip():
            messagebox.showerror("缺欄位", "請輸入 Session Cookie。")
            return
        args = self._common_args()
        limit = self.dl_limit_var.get().strip()
        if limit and limit != "0":
            args += ["-l", limit]
        if self.dl_continue_var.get():
            args.append("-i")
        if self.dl_metadata_var.get():
            args.append("-m")
        if self.dl_thumb_var.get():
            args.append("-t")
        if self.dl_use_server_filenames_var.get():
            args.append("-s")
        args.append(url)
        self._push_url_history(self.dl_url_history, self.dl_url_combo, url)
        try:
            self._save_settings()
        except Exception:
            pass
        self._spawn(args, label="下載")

    def _run_verify(self, auto_fix):
        if self._is_running():
            messagebox.showwarning("執行中", "已有任務在跑，請先停止。")
            return
        url = self.v_url_var.get().strip()
        if not url:
            messagebox.showerror("缺欄位", "請輸入 Fanclub URL。")
            return
        if not self.cookie_var.get().strip():
            messagebox.showerror("缺欄位", "請輸入 Session Cookie。")
            return
        if not self.db_var.get().strip():
            messagebox.showerror("缺欄位", "驗證必須提供資料庫檔（共用設定區）。")
            return
        args = self._common_args()
        limit = self.v_limit_var.get().strip()
        if limit and limit != "0":
            args += ["-l", limit]
        args.append("--verify")
        if not auto_fix:
            args.append("--verify-no-fix")
        json_path = self.v_json_var.get().strip()
        if json_path:
            args += ["--verify-json", json_path]
            self.last_report_path = json_path
        else:
            m = re.search(r"/fanclubs/(\d+)", url)
            if m:
                outdir = self.outdir_var.get().strip() or os.getcwd()
                self.last_report_path = os.path.join(outdir, "verify_{}.json".format(m.group(1)))
        args.append(url)
        self._push_url_history(self.v_url_history, self.v_url_combo, url)
        try:
            self._save_settings()
        except Exception:
            pass
        self._spawn(args, label="驗證 ({})".format("補抓" if auto_fix else "只報告"))

    # ---------------------------------------------------- Subprocess

    def _spawn(self, args, label):
        cmd = child_command(args)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # Don't show a console window for the spawned child on Windows.
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=self.outdir_var.get().strip() or None,
                creationflags=creationflags,
            )
        except Exception as e:
            messagebox.showerror("無法啟動", "啟動 fantiadl 子行程失敗：\n{}".format(e))
            return

        self._append_log("\n>>> [{}] 啟動: {}\n".format(label, " ".join(cmd[len(cmd) - len(args) - 1:])))
        self.status_var.set("執行中: " + label)
        self.stop_btn.configure(state=tk.NORMAL)
        self._set_run_buttons(False)

        self.reader_thread = threading.Thread(target=self._read_proc_output, daemon=True)
        self.reader_thread.start()

    def _read_proc_output(self):
        try:
            if self.proc and self.proc.stdout:
                for line in self.proc.stdout:
                    self.log_queue.put(line)
            if self.proc:
                self.proc.wait()
                rc = self.proc.returncode
                self.log_queue.put("\n<<< 行程結束 (return code {})\n".format(rc))
        except Exception as e:
            self.log_queue.put("\n<<< 讀取輸出時發生錯誤: {}\n".format(e))
        finally:
            self.log_queue.put(("__DONE__",))

    def _drain_log_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__DONE__":
                    self._on_proc_done()
                else:
                    self._append_log(item)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_log_queue)

    def _append_log(self, text):
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def _on_proc_done(self):
        self.status_var.set("閒置")
        self.stop_btn.configure(state=tk.DISABLED)
        self._set_run_buttons(True)
        self.proc = None
        self.reader_thread = None

    def _set_run_buttons(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.dl_btn.configure(state=state)

    def _is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def _stop(self):
        if not self._is_running():
            return
        if not messagebox.askyesno("停止", "確定要中斷目前任務？"):
            return
        try:
            self.proc.terminate()
        except Exception as e:
            self._append_log("無法停止: {}\n".format(e))

    def _on_close(self):
        if self._is_running():
            if not messagebox.askyesno("有任務在跑", "任務尚未結束，仍要關閉視窗（會中斷任務）？"):
                return
            try:
                self.proc.terminate()
            except Exception:
                pass
        try:
            self._save_settings()
        except Exception:
            pass
        self.root.destroy()

    # ---------------------------------------------------- Persistence

    def _on_remember_cookie_toggled(self):
        """When the user un-checks 'remember cookie', wipe the saved cookie
        from disk on the spot rather than waiting for window close."""
        if not self.remember_cookie_var.get():
            try:
                self._save_settings()
            except Exception:
                pass

    def _push_url_history(self, history_list, combobox, url):
        """Move/insert URL to the front of history, dedupe, cap at MAX_URL_HISTORY."""
        if not url:
            return
        if url in history_list:
            history_list.remove(url)
        history_list.insert(0, url)
        del history_list[MAX_URL_HISTORY:]
        try:
            combobox.configure(values=list(history_list))
        except Exception:
            pass

    def _load_settings(self):
        path = settings_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            # Corrupted file: keep defaults but tell the user via log
            self._append_log("讀取設定失敗 ({}): {}\n".format(path, e))
            return
        if not isinstance(data, dict):
            return

        remember = bool(data.get("remember_cookie"))
        self.remember_cookie_var.set(remember)
        if remember and isinstance(data.get("cookie"), str):
            self.cookie_var.set(data["cookie"])

        if isinstance(data.get("output_dir"), str) and data["output_dir"]:
            self.outdir_var.set(data["output_dir"])
        if isinstance(data.get("db_path"), str):
            self.db_var.set(data["db_path"])

        dl = data.get("download") or {}
        if isinstance(dl, dict):
            if isinstance(dl.get("url"), str):
                self.dl_url_var.set(dl["url"])
            hist = dl.get("url_history") or []
            if isinstance(hist, list):
                self.dl_url_history = [u for u in hist if isinstance(u, str)][:MAX_URL_HISTORY]
                self.dl_url_combo.configure(values=list(self.dl_url_history))
            if isinstance(dl.get("limit"), str):
                self.dl_limit_var.set(dl["limit"])
            for key, var in (("continue_on_error", self.dl_continue_var),
                             ("dump_metadata", self.dl_metadata_var),
                             ("thumb", self.dl_thumb_var),
                             ("use_server_filenames", self.dl_use_server_filenames_var)):
                if isinstance(dl.get(key), bool):
                    var.set(dl[key])

        vf = data.get("verify") or {}
        if isinstance(vf, dict):
            if isinstance(vf.get("url"), str):
                self.v_url_var.set(vf["url"])
            hist = vf.get("url_history") or []
            if isinstance(hist, list):
                self.v_url_history = [u for u in hist if isinstance(u, str)][:MAX_URL_HISTORY]
                self.v_url_combo.configure(values=list(self.v_url_history))
            if isinstance(vf.get("json_path"), str):
                self.v_json_var.set(vf["json_path"])
            if isinstance(vf.get("limit"), str):
                self.v_limit_var.set(vf["limit"])

        adv = data.get("advanced") or {}
        if isinstance(adv, dict):
            for key, var in self.adv_vars.items():
                val = adv.get(key)
                if isinstance(val, str):
                    var.set(val)

    def _save_settings(self):
        path = settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
        data = {
            "version": SETTINGS_VERSION,
            "remember_cookie": bool(self.remember_cookie_var.get()),
            # cookie persisted only when opted-in; otherwise stored as empty
            "cookie": self.cookie_var.get() if self.remember_cookie_var.get() else "",
            "output_dir": self.outdir_var.get(),
            "db_path": self.db_var.get(),
            "download": {
                "url": self.dl_url_var.get(),
                "url_history": list(self.dl_url_history),
                "limit": self.dl_limit_var.get(),
                "continue_on_error": bool(self.dl_continue_var.get()),
                "dump_metadata": bool(self.dl_metadata_var.get()),
                "thumb": bool(self.dl_thumb_var.get()),
                "use_server_filenames": bool(self.dl_use_server_filenames_var.get()),
            },
            "verify": {
                "url": self.v_url_var.get(),
                "url_history": list(self.v_url_history),
                "json_path": self.v_json_var.get(),
                "limit": self.v_limit_var.get(),
            },
            "advanced": {k: v.get() for k, v in self.adv_vars.items()},
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            self._append_log("寫入設定失敗 ({}): {}\n".format(path, e))


def main():
    root = tk.Tk()
    try:
        # ttk 系統主題 (Windows: vista, xpnative)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    FantiadlGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
