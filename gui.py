"""双击入口；所有窗口操作均在 Tk 主线程执行。"""
import json
from collections import deque
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import urlsplit

from core import Store, validate_url
from credentials import protect_api_key, unprotect_api_key
from gui_backend import Bridge, GuiRequest, execute

PLATFORMS = {'通用课程（自动识别链接）': 'generic', '超星学习通': 'chaoxing',
             '中国大学 MOOC': 'mooc', 'U校园 AI': 'uai'}
BROWSERS = {'Microsoft Edge': 'msedge', 'Google Chrome': 'chrome'}
OUTCOMES = {'inspected': '检测完成', 'limit': '已达到本次视频数量，进度已保存',
            's': '本课程已结束，进度已保存', 'q': '已停止，进度保留到最后一次观测',
            'cancelled': '已停止', 'error': '需要检查一下'}


@dataclass
class CourseJob:
    request: GuiRequest
    bridge: Bridge = field(default_factory=Bridge)
    worker: object = field(default=None, repr=False)
    status: str = '正在启动…'
    progress_text: str = '正在准备浏览器，请稍候…'
    progress_value: float = 0
    prompt: object = None
    pending_done: object = None
    logs: object = field(default_factory=lambda: deque(maxlen=500))


def app_data_dir():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    return base / 'CourseHelper'


def clock_text(seconds):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return '--:--'
    seconds = int(seconds)
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def automation_values(source):
    """校验界面草稿，不要求 AI 三项同时填写，也不读取或发送网络请求。"""
    try:
        seconds = int(source.document_seconds.get())
    except ValueError:
        raise ValueError('文档每页停留时间须为 3 到 120 秒的整数。') from None
    if not 3 <= seconds <= 120:
        raise ValueError('文档每页停留时间须为 3 到 120 秒的整数。')
    base = source.ai_base_url.get().strip()
    if base:
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme == 'https' and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and not any(char.isspace() or ord(char) < 32 for char in base)
                     and '\\' not in base and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('AI base URL 须为 HTTPS 地址，不能包含账号、密码、查询参数或片段。')
    speech_base = source.speech_base_url.get().strip()
    if speech_base:
        from quiz_ai import _endpoint, QuizAIError
        try:
            _endpoint(speech_base)
        except QuizAIError:
            raise ValueError('语音转写地址须为 HTTPS，不能包含账号、密码、查询参数或片段。') from None
    vision = source.vision_model.get().strip()
    if not vision and base and urlsplit(base).hostname == 'api.deepseek.com':
        vision = 'deepseek-flash'
    return dict(auto_documents=source.auto_documents.get(), auto_quiz=source.auto_quiz.get(),
                quiz_submit=source.quiz_submit.get(),
                document_seconds=seconds, ai_base_url=base,
                ai_model=source.ai_model.get().strip(), ai_api_key=source.ai_api_key.get().strip(),
                vision_model=vision, speech_base_url=speech_base,
                speech_model=source.speech_model.get().strip(), speech_api_key=source.speech_api_key.get().strip())


class LearningSettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title('自动学习设置')
        self.geometry('660x540')
        self.minsize(620, 520)
        self.transient(app.root)
        self.auto_documents = tk.BooleanVar(self, value=app.auto_documents.get())
        self.auto_quiz = tk.BooleanVar(self, value=app.auto_quiz.get())
        self.quiz_submit = tk.BooleanVar(self, value=app.quiz_submit.get())
        self.document_seconds = tk.StringVar(self, value=app.document_seconds.get())
        self.ai_base_url = tk.StringVar(self, value=app.ai_base_url.get())
        self.ai_model = tk.StringVar(self, value=app.ai_model.get())
        self.ai_api_key = tk.StringVar(self, value=app.ai_api_key.get())
        for key in ('vision_model', 'speech_base_url', 'speech_model', 'speech_api_key'):
            setattr(self, key, tk.StringVar(self, value=getattr(app, key).get()))
        self.remember_api_key = tk.BooleanVar(self, value=app.remember_api_key.get())
        frame = ttk.Frame(self, style='Card.TFrame', padding=22)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(frame, text='自动学习文档并翻页', variable=self.auto_documents).grid(
            row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(frame, text='每页停留（秒）').grid(row=1, column=0, sticky='w', pady=(10, 16))
        ttk.Spinbox(frame, from_=3, to=120, textvariable=self.document_seconds, width=10).grid(
            row=1, column=1, sticky='w', pady=(10, 16))
        ttk.Checkbutton(frame, text='使用 AI 处理课程小测', variable=self.auto_quiz).grid(
            row=2, column=0, columnspan=2, sticky='w', pady=(0, 8))
        ttk.Checkbutton(frame, text='自动提交小测（关闭后只填写，等待我检查）',
                        variable=self.quiz_submit).grid(
                            row=3, column=0, columnspan=2, sticky='w', pady=(0, 14))
        notebook = ttk.Notebook(frame)
        notebook.grid(row=4, column=0, columnspan=2, sticky='ew')
        text_page, media_page = [ttk.Frame(notebook, style='Card.TFrame', padding=14) for _ in range(2)]
        notebook.add(text_page, text='文字与图片')
        notebook.add(media_page, text='听力转写')
        for page in (text_page, media_page):
            page.columnconfigure(1, weight=1)
        for row, label, variable in ((0, 'AI base URL', self.ai_base_url),
                                     (1, '文字模型', self.ai_model),
                                     (2, '图片模型', self.vision_model)):
            ttk.Label(text_page, text=label).grid(row=row, column=0, sticky='w', padx=(0, 14), pady=6)
            ttk.Entry(text_page, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=6)
        ttk.Label(text_page, text='DeepSeek 官方地址的图片模型留空时使用 deepseek-flash；'
                  '其他服务请填写支持图片输入的模型。图片与文字共用该地址和密钥。',
                  style='Hint.TLabel', wraplength=500).grid(row=3, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Label(text_page, text='API Key').grid(row=4, column=0, sticky='w', pady=6)
        self.ai_api_key_entry = ttk.Entry(text_page, textvariable=self.ai_api_key, show='*')
        self.ai_api_key_entry.grid(row=4, column=1, sticky='ew', pady=6)
        for row, label, variable in ((0, '转写服务地址', self.speech_base_url),
                                     (1, '转写模型', self.speech_model)):
            ttk.Label(media_page, text=label).grid(row=row, column=0, sticky='w', padx=(0, 14), pady=6)
            ttk.Entry(media_page, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=6)
        ttk.Label(media_page, text='转写 API Key').grid(row=2, column=0, sticky='w', pady=6)
        self.speech_api_key_entry = ttk.Entry(media_page, textvariable=self.speech_api_key, show='*')
        self.speech_api_key_entry.grid(row=2, column=1, sticky='ew', pady=6)
        ttk.Label(media_page, text='填写支持 /audio/transcriptions 的服务地址、模型和该服务的密钥。'
                  '听力先转写成文字，再由文字模型解题。不会把 DeepSeek 密钥自动发给其他服务。'
                  '转写支持可读取的完整音频；分片流、受限音频和语调辨识暂不支持。',
                  style='Hint.TLabel', wraplength=500).grid(row=3, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Checkbutton(frame, text='记住两组 API Key（在本机加密保存）',
                        variable=self.remember_api_key).grid(
                            row=5, column=0, columnspan=2, sticky='w', pady=(12, 0))
        ttk.Label(frame, text='下次打开自动读取；取消勾选并保存可清除已保存的密钥。',
                  style='Hint.TLabel', wraplength=420).grid(
                      row=6, column=0, columnspan=2, sticky='w', pady=(4, 12))
        ttk.Label(frame, text='支持已识别的文字题、图片题、听力转写及拖拽匹配/排序。'
                  '题目文字与图片发送至 AI 服务，听力音频发送至你配置的转写服务。'
                  'AI 可能答错；缺少材料、个人自评或未适配控件会提示你处理。',
                  wraplength=570).grid(
                      row=7, column=0, columnspan=2, sticky='w', pady=(4, 8))
        ttk.Label(frame, text='保存后用于新启动的任务；已运行的任务需停止并重新添加才能使用新设置。',
                  style='Hint.TLabel', wraplength=570).grid(
                      row=8, column=0, columnspan=2, sticky='w')
        actions = ttk.Frame(frame, style='Card.TFrame')
        actions.grid(row=9, column=0, columnspan=2, sticky='sew', pady=(20, 0))
        frame.rowconfigure(9, weight=1)
        ttk.Button(actions, text='保存设置', command=self.save, style='Accent.TButton').pack(
            side='right', padx=(8, 0))
        ttk.Button(actions, text='取消', command=self.destroy).pack(side='right')
        # 依据字体缩放后的实际需求设定窗口，保证底部按钮始终可见。
        self.update_idletasks()
        width = max(660, frame.winfo_reqwidth())
        height = max(540, frame.winfo_reqheight())
        self.minsize(width, height)
        self.geometry(f'{width}x{height}')
        self.grab_set()

    def save(self):
        try:
            values = automation_values(self)
            values['remember_api_key'] = self.remember_api_key.get()
            previous = {key: getattr(self.app, key).get() for key in values}
            for key, value in values.items():
                getattr(self.app, key).set(value)
            try:
                self.app._save_settings(update_key=True)
            except (OSError, ValueError):
                for key, value in previous.items():
                    getattr(self.app, key).set(value)
                raise
        except (ValueError, OSError) as exc:
            messagebox.showerror('请检查设置', str(exc), parent=self)
            return
        if self.remember_api_key.get() and self.ai_api_key.get().strip():
            self.app.append_log('自动学习设置已保存；API Key 已在本机加密保存，下次打开自动读取。')
        else:
            self.app.append_log('自动学习设置已保存；本机未保存 API Key，当前密钥仅用于本次运行。')
        self.destroy()

    def destroy(self):
        app = self.app
        if app is not None and app.settings_window is self:
            app.settings_window = None
        super().destroy()
        # Entry.master 指回窗口层级；断开这个环，让 Tk 变量在主线程释放。
        self.ai_api_key_entry = None
        self.speech_api_key_entry = None
        self.app = None


class App:
    def __init__(self, root, data_dir, runner=execute):
        self.root = root
        self.data_dir = Path(data_dir)
        self.runner = runner
        self.jobs = {}
        self.selected_id = None
        self.prompt_id = None
        self.closing = False
        self.settings_window = None
        self.root.title('网课助手 · 1.6.4')
        self.root.geometry('900x820')
        self.root.minsize(820, 780)
        self.root.configure(bg='#F3F5F8')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.platform = tk.StringVar(value='超星学习通')
        self.platform_hint = tk.StringVar()
        self._platform_trace = self.platform.trace_add('write', self._update_platform_hint)
        self.root.bind('<Destroy>', self._on_root_destroy, add='+')
        self.course_name = tk.StringVar(value='我的课程')
        self.url = tk.StringVar()
        self.maximum = tk.StringVar(value='2')
        self.continuous = tk.BooleanVar(value=True)
        self.resume = tk.BooleanVar(value=True)
        self.background_playback = tk.BooleanVar(value=True)
        self.browser = tk.StringVar(value='Microsoft Edge')
        self.auto_documents = tk.BooleanVar(value=True)
        self.auto_quiz = tk.BooleanVar(value=True)
        self.quiz_submit = tk.BooleanVar(value=True)
        self.document_seconds = tk.StringVar(value='10')
        self.ai_base_url = tk.StringVar()
        self.ai_model = tk.StringVar()
        self.ai_api_key = tk.StringVar()
        for key in ('vision_model', 'speech_base_url', 'speech_model', 'speech_api_key'):
            setattr(self, key, tk.StringVar())
        self.remember_api_key = tk.BooleanVar(value=True)
        self._unreadable_key = None
        self._unreadable_speech_key = None
        self._settings_warning = ''
        self.status = tk.StringVar(value='准备就绪')
        self.progress_text = tk.StringVar(value='选择平台，点击“添加并开始”')
        self._load_settings()
        self._update_platform_hint()
        self._build()
        if self._settings_warning:
            self.status.set(self._settings_warning)
            self.append_log(self._settings_warning)
        self.root.after(100, self.poll)

    def _build(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        # 仅替换复选框标记，让 Windows 绘制熟悉的勾号；保留原有配色和焦点布局。
        if 'vista' in style.theme_names():
            indicator = 'NativeCheckmark.indicator'
            if indicator not in style.element_names():
                style.element_create(indicator, 'from', 'vista', 'Checkbutton.indicator')
            layout = style.layout('TCheckbutton')
            for _, options in layout:
                options['children'] = [
                    (indicator if name == 'Checkbutton.indicator' else name, settings)
                    for name, settings in options.get('children', [])]
            style.layout('TCheckbutton', layout)
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background='#F3F5F8')
        style.configure('Card.TFrame', background='#FFFFFF')
        style.configure('TLabel', background='#FFFFFF', foreground='#263344')
        style.configure('Hint.TLabel', foreground='#687789', font=('Microsoft YaHei UI', 9))
        style.configure('TButton', padding=(13, 8), background='#E8EEF4', foreground='#263344')
        style.configure('Accent.TButton', background='#117C75', foreground='white')
        style.map('Accent.TButton', background=[('active', '#096B65'), ('disabled', '#BBCDCB')])
        style.configure('TCheckbutton', background='#FFFFFF')
        style.configure('TProgressbar', background='#117C75', troughcolor='#E8EEF4')
        header = tk.Frame(self.root, bg='#183448', padx=26, pady=16)
        header.pack(fill='x')
        tk.Label(header, text='网课助手', bg='#183448', fg='white',
                 font=('Microsoft YaHei UI', 23, 'bold')).pack(anchor='w')
        tk.Label(header, text='1  选择课程     →     2  浏览器登录     →     3  开始学习',
                 bg='#183448', fg='#BBD7DF', font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(4, 0))
        body = ttk.Frame(self.root, padding=(22, 16))
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        form = ttk.Frame(body, style='Card.TFrame', padding=16)
        form.grid(row=0, column=0, sticky='ew')
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text='学习平台').grid(row=0, column=0, sticky='w', padx=(0, 10))
        platform = ttk.Combobox(form, textvariable=self.platform,
                                values=list(PLATFORMS), state='readonly', width=20)
        platform.grid(row=0, column=1, sticky='ew', padx=(0, 24))
        ttk.Label(form, text='课程名称').grid(row=0, column=2, sticky='w', padx=(0, 10))
        name = ttk.Entry(form, textvariable=self.course_name, width=22)
        name.grid(row=0, column=3, sticky='ew')
        ttk.Label(form, text='课程网址').grid(row=1, column=0, sticky='w', pady=(14, 0))
        url = ttk.Entry(form, textvariable=self.url)
        url.grid(row=1, column=1, columnspan=3, sticky='ew', pady=(14, 0))
        ttk.Label(form, text='可留空，在浏览器中手动打开课程；不同课程请使用不同名称。',
                  style='Hint.TLabel').grid(row=2, column=1, columnspan=3, sticky='w', pady=(4, 12))
        self.continuous_toggle = ttk.Checkbutton(form, text='整门课连续学习',
                                                 variable=self.continuous,
                                                 command=self._update_maximum_state)
        self.continuous_toggle.grid(row=3, column=0, columnspan=2, sticky='w')
        ttk.Label(form, text='使用浏览器').grid(row=3, column=2, sticky='w')
        browser = ttk.Combobox(form, textvariable=self.browser,
                               values=list(BROWSERS), state='readonly', width=22)
        browser.grid(row=3, column=3, sticky='ew')
        ttk.Label(form, text='本次视频数').grid(row=4, column=0, sticky='w', pady=(12, 0))
        self.maximum_input = ttk.Spinbox(form, from_=1, to=1000, textvariable=self.maximum, width=10)
        self.maximum_input.grid(row=4, column=1, sticky='w', pady=(12, 0))
        background = ttk.Checkbutton(form, text='后台播放兼容（切换软件 / 最小化）',
                                     variable=self.background_playback)
        background.grid(row=4, column=2, columnspan=2, sticky='w', pady=(12, 0))
        self._update_maximum_state()
        resume = ttk.Checkbutton(form, text='网址留空时，恢复同名课程上次的章节', variable=self.resume)
        resume.grid(row=5, column=0, columnspan=4, sticky='w', pady=(12, 0))
        ttk.Label(form, textvariable=self.platform_hint, style='Hint.TLabel',
                  wraplength=740).grid(row=6, column=0, columnspan=4, sticky='w', pady=(8, 0))
        self.inputs = [(platform, 'readonly'), (name, 'normal'), (url, 'normal'),
                       (self.maximum_input, 'normal'), (self.continuous_toggle, 'normal'),
                       (browser, 'readonly'), (resume, 'normal'), (background, 'normal')]
        actions = ttk.Frame(body, padding=(0, 12))
        actions.grid(row=1, column=0, sticky='ew')
        self.start_button = ttk.Button(actions, text='添加并开始', style='Accent.TButton',
                                       command=lambda: self.start('run'))
        self.start_button.pack(side='left', padx=(0, 8))
        self.inspect_button = ttk.Button(actions, text='检测页面', command=lambda: self.start('inspect'))
        self.inspect_button.pack(side='left', padx=(0, 8))
        self.settings_button = ttk.Button(actions, text='自动学习设置', command=self.show_settings)
        self.settings_button.pack(side='left', padx=(8, 0))
        ttk.Button(actions, text='查看进度记录', command=self.show_records).pack(side='right')
        task_frame = ttk.Frame(body, style='Card.TFrame', padding=(14, 8))
        task_frame.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        task_actions = ttk.Frame(task_frame, style='Card.TFrame')
        task_actions.pack(fill='x', pady=(0, 6))
        ttk.Label(task_actions, text='课程任务 · 选择一行查看详情').pack(side='left')
        self.stop_all_button = ttk.Button(task_actions, text='全部停止', command=self.stop_all,
                                          state='disabled')
        self.stop_all_button.pack(side='right')
        self.stop_button = ttk.Button(task_actions, text='停止所选', command=self.stop, state='disabled')
        self.stop_button.pack(side='right', padx=(0, 8))
        task_table = ttk.Frame(task_frame, style='Card.TFrame')
        task_table.pack(fill='x')
        self.task_table = ttk.Treeview(task_table, columns=('platform', 'name', 'status', 'progress'),
                                       show='headings', selectmode='browse', height=4)
        for key, title, width in [('platform', '平台', 115), ('name', '课程', 140),
                                  ('status', '状态', 280), ('progress', '视频进度', 100)]:
            self.task_table.heading(key, text=title)
            self.task_table.column(key, width=width, minwidth=70)
        task_scroll = ttk.Scrollbar(task_table, orient='vertical', command=self.task_table.yview)
        self.task_table.configure(yscrollcommand=task_scroll.set)
        self.task_table.pack(side='left', fill='x', expand=True)
        task_scroll.pack(side='right', fill='y')
        self.task_table.bind('<<TreeviewSelect>>', self._on_selection)
        progress = ttk.Frame(body, style='Card.TFrame', padding=14)
        progress.grid(row=3, column=0, sticky='ew')
        ttk.Label(progress, textvariable=self.status, wraplength=740,
                  font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w')
        self.progress = ttk.Progressbar(progress, maximum=100, mode='determinate')
        self.progress.pack(fill='x', pady=(8, 5))
        ttk.Label(progress, textvariable=self.progress_text, style='Hint.TLabel').pack(anchor='w')
        self.prompt_frame = ttk.Frame(body, style='Card.TFrame', padding=14)
        self.prompt_frame.grid(row=4, column=0, sticky='ew', pady=(10, 0))
        self.prompt_title = ttk.Label(self.prompt_frame, font=('Microsoft YaHei UI', 11, 'bold'))
        self.prompt_title.pack(anchor='w')
        self.prompt_message = ttk.Label(self.prompt_frame, wraplength=740)
        self.prompt_message.pack(anchor='w', pady=(5, 8))
        self.prompt_actions = ttk.Frame(self.prompt_frame, style='Card.TFrame')
        self.prompt_actions.pack(fill='x')
        self.prompt_frame.grid_remove()
        logs = ttk.Frame(body, padding=(0, 12, 0, 0))
        logs.grid(row=5, column=0, sticky='nsew')
        self.log = scrolledtext.ScrolledText(logs, height=5, wrap='word', state='disabled',
                                            font=('Microsoft YaHei UI', 9), relief='flat',
                                            bg='#EAF0F4', fg='#405162', padx=10, pady=8)
        self.log.pack(fill='both', expand=True)
        ttk.Label(body, text='登录和验证码需人工处理；AI 未获得小测答案时会提示。学习进度以平台显示为准。',
                  background='#F3F5F8', style='Hint.TLabel').grid(row=6, column=0, sticky='w', pady=(8, 0))
        self.append_log('点击“添加并开始”启动一门课；运行期间可继续添加其他平台或课程。每门课独立登录。')
        self.root.update_idletasks()
        width = max(900, body.winfo_reqwidth())
        height = max(820, self.root.winfo_reqheight())
        self.root.minsize(width, height)
        self.root.geometry(f'{width}x{height}')

    def _load_settings(self):
        try:
            saved = json.loads((self.data_dir / 'settings.json').read_text(encoding='utf-8'))
            if saved.get('platform') in PLATFORMS:
                self.platform.set(saved['platform'])
            if saved.get('browser') in BROWSERS:
                self.browser.set(saved['browser'])
            if isinstance(saved.get('course_name'), str):
                self.course_name.set(saved['course_name'])
            if type(saved.get('maximum')) is int and 1 <= saved['maximum'] <= 1000:
                self.maximum.set(str(saved['maximum']))
            if type(saved.get('resume')) is bool:
                self.resume.set(saved['resume'])
            for key in ('continuous', 'auto_documents', 'auto_quiz', 'quiz_submit', 'background_playback',
                        'remember_api_key'):
                if type(saved.get(key)) is bool:
                    getattr(self, key).set(saved[key])
            if type(saved.get('document_seconds')) is int and 3 <= saved['document_seconds'] <= 120:
                self.document_seconds.set(str(saved['document_seconds']))
            for key in ('ai_base_url', 'ai_model', 'vision_model', 'speech_base_url', 'speech_model'):
                if isinstance(saved.get(key), str):
                    getattr(self, key).set(saved[key])
            protected = saved.get('ai_api_key_protected')
            self._unreadable_key = None
            self._settings_warning = ''
            if self.remember_api_key.get() and protected:
                try:
                    self.ai_api_key.set(unprotect_api_key(protected))
                except (OSError, ValueError):
                    self._unreadable_key = protected
                    self._settings_warning = '已保存的 API Key 无法读取，请在“自动学习设置”中重新填写并保存。'
            protected_speech = saved.get('speech_api_key_protected')
            self._unreadable_speech_key = None
            if self.remember_api_key.get() and protected_speech:
                try:
                    self.speech_api_key.set(unprotect_api_key(protected_speech))
                except (OSError, ValueError):
                    self._unreadable_speech_key = protected_speech
                    self._settings_warning += ' 已保存的语音转写密钥无法读取，请重新填写并保存。'
        except (OSError, ValueError, AttributeError):
            pass

    def _save_settings(self, *, update_key=False):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 连续模式不使用视频数；即使旧输入无效也不阻断整门课学习。
        try:
            maximum = int(self.maximum.get())
        except ValueError:
            maximum = 2
        if not 1 <= maximum <= 1000:
            maximum = 2
        saved = dict(platform=self.platform.get(), browser=self.browser.get(),
                     course_name=self.course_name.get().strip(), maximum=maximum,
                     resume=self.resume.get(), continuous=self.continuous.get(),
                     background_playback=self.background_playback.get(),
                     auto_documents=self.auto_documents.get(), auto_quiz=self.auto_quiz.get(),
                     quiz_submit=self.quiz_submit.get(),
                     document_seconds=int(self.document_seconds.get()),
                     ai_base_url=self.ai_base_url.get().strip(), ai_model=self.ai_model.get().strip(),
                     remember_api_key=self.remember_api_key.get())
        for name in ('vision_model', 'speech_base_url', 'speech_model'):
            saved[name] = getattr(self, name).get().strip()
        key = self.ai_api_key.get().strip()
        if self.remember_api_key.get():
            if key:
                saved['ai_api_key_protected'] = protect_api_key(key)
            elif self._unreadable_key is not None and not update_key:
                # 启动视频任务也会保存设置；不能因此抹去尚未成功读取的密钥。
                saved['ai_api_key_protected'] = self._unreadable_key
        speech_key = self.speech_api_key.get().strip()
        if self.remember_api_key.get():
            if speech_key:
                saved['speech_api_key_protected'] = protect_api_key(speech_key)
            elif self._unreadable_speech_key is not None and not update_key:
                saved['speech_api_key_protected'] = self._unreadable_speech_key
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.data_dir,
                                             prefix='.settings-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(saved, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.data_dir / 'settings.json')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if update_key or key or not self.remember_api_key.get():
            self._unreadable_key = None
        if update_key or speech_key or not self.remember_api_key.get():
            self._unreadable_speech_key = None
        if self._unreadable_key is None and self._unreadable_speech_key is None:
            if self._settings_warning and self.status.get() == self._settings_warning:
                self.status.set('自动学习设置已保存')
            self._settings_warning = ''

    def _update_platform_hint(self, *_):
        if PLATFORMS.get(self.platform.get()) == 'generic':
            self.platform_hint.set('粘贴 HTTPS 课程链接；已知平台自动适配，其他站点尝试通用识别。'
                                   '目录不识别时可在浏览器中定位当前项和下一项。')
        elif PLATFORMS.get(self.platform.get()) == 'uai':
            self.platform_hint.set('U校园 AI：进入具体课件后再继续；支持已识别的同页文字选择、填空、词库和阅读题。'
                                   '图片和听力需配置对应模型；口语、自评及未适配分页/控件需手动处理。')
        else:
            self.platform_hint.set('请在浏览器中打开具体课程章节，再点击“我已打开课程”。')

    def _on_root_destroy(self, event):
        if event.widget is self.root and self._platform_trace is not None:
            # Variable 的 Tcl 回调持有 App；窗口销毁不会自动删除该变量回调。
            self.platform.trace_remove('write', self._platform_trace)
            self._platform_trace = None

    def _update_maximum_state(self):
        self.maximum_input.configure(state='disabled' if self.continuous.get() else 'normal')

    def show_settings(self):
        if self.closing:
            return None
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
        else:
            self.settings_window = LearningSettingsDialog(self)
        return self.settings_window

    @property
    def selected_job(self):
        return self.jobs.get(self.selected_id)

    @property
    def worker(self):
        return self.selected_job.worker if self.selected_job else None

    @property
    def bridge(self):
        return self.selected_job.bridge if self.selected_job else None

    def append_log(self, message, job=None):
        job = job or self.selected_job
        if job is not None:
            job.logs.append(str(message))
            if job is not self.selected_job:
                return
        self.log.configure(state='normal')
        self.log.insert('end', str(message) + '\n')
        # 长时间运行时限制显示行数；完整记录仍保存在本地日志。
        if int(self.log.index('end-1c').split('.')[0]) > 500:
            self.log.delete('1.0', '100.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _refresh_buttons(self):
        for widget, normal in self.inputs:
            widget.configure(state='disabled' if self.closing else normal)
        for button in (self.start_button, self.inspect_button, self.settings_button):
            button.configure(state='disabled' if self.closing else 'normal')
        job = self.selected_job
        stoppable = job and job.worker is not None and not job.bridge.stopped
        self.stop_button.configure(state='normal' if stoppable else 'disabled')
        any_running = any(job.worker is not None and not job.bridge.stopped
                          for job in self.jobs.values())
        self.stop_all_button.configure(state='normal' if any_running else 'disabled')
        if not self.closing:
            self._update_maximum_state()

    def _update_row(self, job):
        platform = next(label for label, code in PLATFORMS.items() if code == job.request.platform)
        self.task_table.item(job.request.task_id, values=(platform,
            job.request.task_id.split(':', 1)[-1], job.status, f'{job.progress_value:.0f}%'))

    def _on_selection(self, _event=None):
        selection = self.task_table.selection()
        if selection and selection[0] != self.selected_id:
            self.select_job(selection[0])

    def select_job(self, task_id):
        if task_id not in self.jobs:
            return
        self.selected_id = task_id
        if self.task_table.selection() != (task_id,):
            self.task_table.selection_set(task_id)
        self.task_table.see(task_id)
        job = self.selected_job
        self.status.set(job.status)
        self.progress_text.set(job.progress_text)
        self.progress['value'] = job.progress_value
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.insert('end', '\n'.join(job.logs) + ('\n' if job.logs else ''))
        self.log.see('end')
        self.log.configure(state='disabled')
        self.clear_prompt()
        if job.prompt and not job.bridge.stopped and not self.closing:
            self.show_prompt(job.prompt)
        self._refresh_buttons()

    def start(self, command):
        if self.closing:
            return
        try:
            platform = PLATFORMS[self.platform.get()]
            name = self.course_name.get().strip()
            if not name:
                raise ValueError('请给这门课程取一个名称，方便下次恢复。')
            try:
                maximum = 0 if self.continuous.get() else int(self.maximum.get())
            except ValueError:
                raise ValueError('本次视频数须为 1 到 1000。') from None
            if not self.continuous.get() and not 1 <= maximum <= 1000:
                raise ValueError('本次视频数须为 1 到 1000。')
            url = self.url.get().strip()
            if url:
                validate_url(url, platform)
            request = GuiRequest(command, platform, f'{platform}:{name}', url,
                                 maximum, self.resume.get(), BROWSERS[self.browser.get()],
                                 background_playback=self.background_playback.get(),
                                 **automation_values(self))
            existing = self.jobs.get(request.task_id)
            if existing is not None and existing.worker is not None:
                raise ValueError('这门课程正在运行。请先停止该任务，或为另一门课程填写不同名称。')
            self._save_settings()
        except (ValueError, KeyError, OSError) as exc:
            messagebox.showerror('请检查设置', str(exc), parent=self.root)
            return
        job = CourseJob(request)
        self.jobs[request.task_id] = job
        if not self.task_table.exists(request.task_id):
            self.task_table.insert('', 'end', iid=request.task_id)
        job.worker = threading.Thread(target=self.runner,
                                      args=(request, job.bridge, self.data_dir), daemon=False)
        self._update_row(job)
        self.select_job(request.task_id)
        try:
            job.worker.start()
        except RuntimeError:
            job.worker = None
            job.status = '工作线程启动失败，请重试。'
            self._update_row(job)
            self.select_job(request.task_id)

    def _stop_job(self, job):
        if job.worker is not None:
            job.bridge.stop()
            job.prompt = None
            job.status = '正在停止并关闭助手浏览器，请稍候…'
            self._update_row(job)
            if job is self.selected_job:
                self.clear_prompt()
                self.status.set(job.status)

    def stop(self):
        if self.selected_job:
            self._stop_job(self.selected_job)
        self._refresh_buttons()

    def stop_all(self):
        for job in self.jobs.values():
            self._stop_job(job)
        self._refresh_buttons()

    def clear_prompt(self):
        self.prompt_id = None
        for widget in self.prompt_actions.winfo_children():
            widget.destroy()
        self.prompt_frame.grid_remove()

    def show_prompt(self, event):
        self.clear_prompt()
        self.prompt_id = event['request_id']
        task_id, request_id = self.selected_id, self.prompt_id
        self.prompt_title.configure(text=event['title'])
        self.prompt_message.configure(text=event['message'])
        choices = event['choices']
        if len(choices) > 3:
            selected = ttk.Combobox(self.prompt_actions, state='readonly', width=62,
                                    values=[f'{i+1}. {label}' for i, (_, label) in enumerate(choices)])
            selected.current(0)
            selected.pack(side='left', fill='x', expand=True, padx=(0, 8))
            ttk.Button(self.prompt_actions, text='确认选择', style='Accent.TButton',
                       command=lambda: self.answer(choices[selected.current()][0],
                                                   task_id, request_id)).pack(side='left')
        else:
            for index, (key, label) in enumerate(choices):
                ttk.Button(self.prompt_actions, text=label,
                           style='Accent.TButton' if index == 0 else 'TButton',
                           command=lambda value=key: self.answer(value, task_id, request_id)).pack(
                               side='left', padx=(0, 8))
        self.prompt_frame.grid()

    def answer(self, value, task_id=None, request_id=None):
        # 回调绑定创建时的任务与提示，切换行后不能把旧操作送到另一门课。
        if task_id is not None and (task_id != self.selected_id or request_id != self.prompt_id):
            return
        if self.bridge is not None and self.prompt_id is not None:
            if self.bridge.reply(self.prompt_id, value):
                self.clear_prompt()
                self.selected_job.prompt = None
                self.selected_job.status = '正在继续…'
                self.status.set(self.selected_job.status)
                self._update_row(self.selected_job)

    def handle_event(self, event, job=None):
        job = job or self.selected_job
        if job is None:
            return
        kind = event['kind']
        if kind == 'log':
            self.append_log(event['message'], job)
        elif kind == 'status':
            if not job.bridge.stopped:
                job.status = event['message']
        elif kind == 'prompt':
            if not self.closing and not job.bridge.stopped:
                job.prompt = event
                job.status = '等待操作：' + event['title']
                self.append_log(event['title'] + '：' + event['message'], job)
                if job is self.selected_job:
                    self.show_prompt(event)
        elif kind == 'progress':
            current, duration = event['current'], event['duration']
            if math.isfinite(current) and math.isfinite(duration) and duration > 0:
                job.progress_value = max(0, min(100, current / duration * 100))
                job.progress_text = (f'当前视频 {clock_text(current)} / {clock_text(duration)}'
                                     f'    ·    本次已自然结束 {event["completed"]} 个视频')
            else:
                job.progress_value = 0
                job.progress_text = '视频正在加载，暂未读取到时长'
        elif kind == 'inspection':
            report = event['report']
            count = sum(len(frame.get('videos', [])) for frame in report.get('frames', []))
            self.append_log(f'检测结果：找到 {count} 个可见视频；'
                            f'下一项：{"已找到" if report.get("has_next") else "未找到"}。', job)
            for frame in report.get('frames', []):
                quiz = frame.get('quiz')
                if quiz:
                    self.append_log(f'题目识别：{quiz["questions"]} 道题，'
                                    f'{quiz["blanks"]} 个填空。{quiz["message"]}', job)
            directory = report.get('directory')
            if directory:
                self.append_log(f'目录：第 {directory["current"]} / {directory["total"]} 项，'
                                f'当前为“{directory["title"]}”。', job)
            self.append_log(report.get('guard') or '未检测到需要人工处理的登录或验证控件。', job)
            if report.get('next_warning'):
                self.append_log(report['next_warning'], job)
        elif kind == 'done':
            job.pending_done = event
            job.prompt = None
            if job is self.selected_job:
                self.clear_prompt()
        self._update_row(job)
        if job is self.selected_job:
            self.status.set(job.status)
            self.progress['value'] = job.progress_value
            self.progress_text.set(job.progress_text)

    def poll(self):
        for job in self.jobs.values():
            # 每轮设上限，避免某个任务大量日志阻塞其他任务和界面。
            for _ in range(100):
                try:
                    self.handle_event(job.bridge.events.get_nowait(), job)
                except queue.Empty:
                    break
            if job.worker is not None and not job.worker.is_alive() and job.bridge.events.empty():
                event = job.pending_done or {'kind': 'done', 'outcome': 'error',
                    'message': '运行已中止，请重新打开课程后再试。'}
                job.worker = None
                job.pending_done = None
                job.prompt = None
                job.status = event.get('message') or OUTCOMES.get(event['outcome'], '已结束')
                self.append_log(job.status, job)
                self._update_row(job)
                if job is self.selected_job:
                    self.clear_prompt()
                    self.status.set(job.status)
        self._refresh_buttons()
        if self.closing and all(job.worker is None for job in self.jobs.values()):
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def show_records(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            store = Store(self.data_dir / 'progress.sqlite3')
            try:
                rows = store.rows()
            finally:
                store.close()
        except Exception:
            messagebox.showerror('记录暂不可用', '暂时无法读取进度记录，请稍后重试。', parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title('本地进度记录')
        window.geometry('760x420')
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='记录每个视频最后一次观测到的进度；平台是否完成，以网页显示为准。',
                  background='#F3F5F8').pack(anchor='w', pady=(0, 12))
        table = ttk.Treeview(frame, columns=('course', 'progress', 'status'), show='headings')
        for key, title, width in [('course', '课程', 270), ('progress', '视频进度', 160), ('status', '状态', 190)]:
            table.heading(key, text=title)
            table.column(key, width=width)
        table.pack(fill='both', expand=True)
        statuses = {'observing': '观测中', 'starting': '开始播放',
                    'needs_user': '等待人工处理', 'video_ended': '视频已自然结束'}
        for row in rows:
            table.insert('', 'end', values=(row['task_id'].split(':', 1)[-1],
                         f'{clock_text(row["current"])} / {clock_text(row["duration"])}',
                         statuses.get(row['status'], row['status'])))
        if not rows:
            ttk.Label(frame, text='暂无记录，开始学习后会自动保存。', background='#F3F5F8').pack(pady=12)
        ttk.Button(frame, text='关闭', command=window.destroy).pack(anchor='e', pady=(12, 0))

    def close(self):
        self.closing = True
        if any(job.worker is not None for job in self.jobs.values()):
            self.stop_all()
        else:
            self.root.destroy()


def main():
    # Windows 高 DPI 屏幕上保持文字清晰。
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        # 使用临时测试密钥验证打包内的加密与配置恢复，不访问真实凭据或联网。
        root.withdraw()
        with tempfile.TemporaryDirectory() as directory:
            app = App(root, Path(directory))
            root.update_idletasks()
            app.ai_api_key.set('self-test-synthetic-api-key')
            app.speech_api_key.set('self-test-synthetic-speech-key')
            app.background_playback.set(False)
            app._save_settings(update_key=True)
            app.ai_api_key.set('')
            app.speech_api_key.set('')
            app.background_playback.set(True)
            app._load_settings()
            assert app.background_playback.get() is False
            assert app.ai_api_key.get() == 'self-test-synthetic-api-key'
            assert app.speech_api_key.get() == 'self-test-synthetic-speech-key'
            assert 'self-test-synthetic-api-key' not in (Path(directory) / 'settings.json').read_text(
                encoding='utf-8')
            assert 'self-test-synthetic-speech-key' not in (Path(directory) / 'settings.json').read_text(
                encoding='utf-8')
            import quiz_media, quiz_drag, quiz_speech
            from quiz_ocr import read_text
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                assert playwright.chromium.name == 'chromium'
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page()
                page.set_content('<p style="font:28px Microsoft YaHei;width:600px">语音识别（ASR）</p>')
                recognized = read_text(page.locator('p'))
                assert '语音识别' in recognized and 'ASR' in recognized
                page.set_content('<span style="font:16px Microsoft YaHei">错</span>')
                assert read_text(page.locator('span')) == '错'
                browser.close()
            store = Store(Path(directory) / 'progress.sqlite3')
            assert store.rows() == []
            store.close()
            Path(sys.argv[2]).write_text(json.dumps({'ok': True, 'version': '1.6.4', 'tk': tk.TkVersion,
                                                   'playwright_driver': True,
                                                   'api_key_encryption': True,
                                                   'speech_key_encryption': True,
                                                   'multimedia_modules': True,
                                                   'windows_ocr': True,
                                                   'background_settings': True,
                                                   'sqlite': True}), encoding='utf-8')
        root.destroy()
        return
    App(root, app_data_dir())
    root.mainloop()


if __name__ == '__main__':
    main()
