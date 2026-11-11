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

from core import Store, validate_url, validate_playback_rate
from credentials import protect_api_key, unprotect_api_key
from gui_backend import Bridge, GuiRequest, execute, task_directory

PLATFORMS = {'通用课程（自动识别链接）': 'generic', '超星学习通': 'chaoxing',
             '中国大学 MOOC': 'mooc', 'U校园 AI': 'uai'}
BROWSERS = {'Microsoft Edge': 'msedge', 'Google Chrome': 'chrome'}
API_FIELDS = ('ai_base_url', 'ai_model', 'vision_model', 'ai_review_model',
              'speech_base_url', 'speech_model', 'remember_api_key',
              'ai_api_key_protected', 'speech_api_key_protected')
OUTCOMES = {'inspected': '检测完成', 'limit': '已达到本次视频数量，进度已保存',
            's': '本课程已结束，进度已保存', 'q': '已停止，进度保留到最后一次观测',
            'ai_disagreement': '本课程已终止，双方答案有分歧。点击「AI 结果」查看逐题比较。',
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
    quiz_report: str = ''
    pending_done: object = None
    logs: object = field(default_factory=lambda: deque(maxlen=500))


def app_data_dir():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    return base / 'CourseHelper'


def read_settings(path):
    try:
        saved = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError('配置文件格式无法读取，已保留原文件。请修复配置后重新打开助手。') from None
    if not isinstance(saved, dict):
        raise ValueError('配置文件格式无效，已保留原文件。请修复配置后重新打开助手。')
    return saved


def api_fields(saved):
    return {name: saved[name] for name in API_FIELDS if name in saved}


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
    try:
        rate = validate_playback_rate(float(source.playback_rate.get()))
    except (ValueError, TypeError):
        raise ValueError('视频倍速须为 1 到 4 之间的数字。') from None
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
        from quiz_ai import _endpoint, QuizAIError
        try:
            _endpoint(base)
        except QuizAIError as exc:
            raise ValueError(str(exc)) from None
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
    if source.quiz_review_provider.get() not in ('豆包网页', '原 API 多轮'):
        raise ValueError('请选择有效的交叉复核方式。')
    return dict(auto_documents=source.auto_documents.get(), auto_quiz=source.auto_quiz.get(),
                quiz_review_provider=source.quiz_review_provider.get(),
                quiz_submit=source.quiz_submit.get(), playback_rate=rate,
                quiz_cross_check=source.quiz_cross_check.get(), ai_review_model=source.ai_review_model.get().strip(),
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
        self.quiz_cross_check = tk.BooleanVar(self, value=app.quiz_cross_check.get())
        self.ai_review_model = tk.StringVar(self, value=app.ai_review_model.get())
        self.quiz_review_provider = tk.StringVar(self, value=app.quiz_review_provider.get())
        self.document_seconds = tk.StringVar(self, value=app.document_seconds.get())
        self.playback_rate = tk.StringVar(self, value=app.playback_rate.get())
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
        timing = ttk.Frame(frame, style='Card.TFrame')
        timing.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(10, 16))
        ttk.Label(timing, text='每页停留（秒）').grid(row=0, column=0, sticky='w')
        ttk.Spinbox(timing, from_=3, to=120, textvariable=self.document_seconds, width=7).grid(
            row=0, column=1, sticky='w', padx=(8, 22))
        ttk.Label(timing, text='视频倍速').grid(row=0, column=2, sticky='w')
        ttk.Combobox(timing, textvariable=self.playback_rate, state='readonly',
                     values=('1', '1.25', '1.5', '2', '3', '4'), width=7).grid(
            row=0, column=3, sticky='w', padx=(8, 0))
        ttk.Label(timing, text='没有倍速按钮也会尝试设置；实际速度见运行状态，平台计时可能不同。',
                  style='Hint.TLabel', wraplength=570).grid(
                      row=1, column=0, columnspan=4, sticky='w', pady=(5, 0))
        ttk.Checkbutton(frame, text='使用 AI 处理课程小测', variable=self.auto_quiz).grid(
            row=2, column=0, columnspan=2, sticky='w', pady=(0, 8))
        ttk.Checkbutton(frame, text='自动提交小测（含交卷确认；关闭后只填写）',
                        variable=self.quiz_submit).grid(
                            row=3, column=0, columnspan=2, sticky='w', pady=(0, 14))
        notebook = ttk.Notebook(frame)
        notebook.grid(row=4, column=0, columnspan=2, sticky='ew')
        text_page, media_page, review_page = [ttk.Frame(notebook, style='Card.TFrame', padding=14) for _ in range(3)]
        notebook.add(text_page, text='截图答题')
        notebook.add(media_page, text='听力转写')
        notebook.add(review_page, text='交叉复核')
        for page in (text_page, media_page, review_page):
            page.columnconfigure(1, weight=1)
        ttk.Checkbutton(review_page, text='启用交叉复核',
                        variable=self.quiz_cross_check).grid(row=0, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Label(review_page, text='复核方式').grid(row=1, column=0, sticky='w', pady=6)
        ttk.Combobox(review_page, textvariable=self.quiz_review_provider, state='readonly',
                     values=('豆包网页', '原 API 多轮')).grid(row=1, column=1, sticky='ew', pady=6)
        ttk.Label(review_page, text='API 复核图片模型').grid(row=2, column=0, sticky='w', pady=6, padx=(0, 14))
        ttk.Entry(review_page, textvariable=self.ai_review_model).grid(row=2, column=1, sticky='ew', pady=6)
        ttk.Label(review_page, text='豆包网页：原 AI 先答，再把原截图发到豆包新对话，全部一致才填写和提交。'
                  '首次须在专用窗口登录，之后沿用登录状态；多个课程排队复核。无需豆包 API Key。'
                  '网页必须能够访问，助手不会修改 VPN。', style='Hint.TLabel', wraplength=500).grid(
                      row=3, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Label(review_page, text='原 API 多轮：2–3 轮独立作答；上面的模型留空沿用原图片模型。'
                  '两种模式都会增加等待时间；答案一致仍可能答错。豆包网页接入尚待真实网络验证。',
                  wraplength=500).grid(row=4, column=0, columnspan=2, sticky='w', pady=6)
        for row, label, variable in ((0, 'AI base URL', self.ai_base_url),
                                     (1, '旧版文字模型', self.ai_model),
                                     (2, '图片模型', self.vision_model)):
            ttk.Label(text_page, text=label).grid(row=row, column=0, sticky='w', padx=(0, 14), pady=6)
            ttk.Entry(text_page, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=6)
        ttk.Label(text_page, text='DeepSeek 官方地址的图片模型留空时使用 deepseek-flash；'
                  '其他服务请填写支持图片输入的模型。题目直接截图，沿用此处密钥。',
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
                  '听力转写后与题目截图一起交给图片模型。不会把 DeepSeek 密钥自动发给其他服务。'
                  '转写支持可读取的完整音频；分片流、受限音频和语调辨识暂不支持。',
                  style='Hint.TLabel', wraplength=500).grid(row=3, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Checkbutton(frame, text='记住两组 API Key（在本机加密保存）',
                        variable=self.remember_api_key).grid(
                            row=5, column=0, columnspan=2, sticky='w', pady=(12, 0))
        ttk.Label(frame, text='下次打开自动读取；取消勾选并保存可清除已保存的密钥。',
                  style='Hint.TLabel', wraplength=420).grid(
                      row=6, column=0, columnspan=2, sticky='w', pady=(4, 12))
        ttk.Label(frame, text='题目和材料截图直接发送至图片模型，不调用本机文字识别。'
                  '支持已适配的选择、填空与拖拽控件；听力音频发送至配置的转写服务。'
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
        elif self.app._unreadable_key is not None:
            self.app.append_log('原有 API Key 暂时无法读取，加密记录已保留。')
        else:
            self.app.append_log('自动学习设置已保存；本机未保存 API Key，当前密钥仅用于本次运行。')
        self.app.append_log('新设置用于新启动的任务。请停止旧任务并重新添加；'
                            '仅点“已处理，继续”会沿用旧任务设置。')
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
        self.root.title('网课助手 · 1.6.48 豆包复核试用版')
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
        self.quiz_cross_check = tk.BooleanVar(value=True)
        self.ai_review_model = tk.StringVar()
        self.quiz_review_provider = tk.StringVar(value='豆包网页')
        self.document_seconds = tk.StringVar(value='10')
        self.playback_rate = tk.StringVar(value='2')
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
        ttk.Button(task_actions, text='截图说明', command=self.show_screenshot_help).pack(
            side='right', padx=(0, 8))
        ttk.Button(task_actions, text='AI 结果', command=self.show_ai_results).pack(side='right', padx=(0, 8))
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
        self.prompt_report = scrolledtext.ScrolledText(self.prompt_frame, height=3, wrap='word',
            state='disabled', font=('Microsoft YaHei UI', 9))
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
        self._loaded_api_fields = None
        try:
            saved = read_settings(self.data_dir / 'settings.json')
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
            for key in ('continuous', 'auto_documents', 'auto_quiz', 'quiz_submit', 'quiz_cross_check', 'background_playback',
                        'remember_api_key'):
                if type(saved.get(key)) is bool:
                    getattr(self, key).set(saved[key])
            if type(saved.get('document_seconds')) is int and 3 <= saved['document_seconds'] <= 120:
                self.document_seconds.set(str(saved['document_seconds']))
            try:
                self.playback_rate.set(f"{validate_playback_rate(saved.get('playback_rate')):g}")
            except ValueError:
                pass
            for key in ('ai_base_url', 'ai_model', 'vision_model', 'speech_base_url', 'speech_model', 'ai_review_model'):
                if isinstance(saved.get(key), str):
                    getattr(self, key).set(saved[key])
            if saved.get('quiz_review_provider') in ('豆包网页', '原 API 多轮'):
                self.quiz_review_provider.set(saved['quiz_review_provider'])
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
            self._loaded_api_fields = api_fields(saved)
        except (OSError, ValueError):
            self._settings_warning = '配置文件未能读取，原文件已保留。请检查配置后重新打开助手。'

    def _save_settings(self, *, update_key=False):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 原子替换保护单次写入；独立锁保护多个窗口的读取、对照和替换。
        with (self.data_dir / '.settings.lock').open('a+b') as lock:
            lock.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('另一个窗口正在保存设置，请稍后重试。') from None
            current = read_settings(self.data_dir / 'settings.json')
            if self._loaded_api_fields is None or api_fields(current) != self._loaded_api_fields:
                raise ValueError('配置文件已变化或先前未能读取。请重新打开助手，载入最新设置。')
            self._write_settings(current, update_key)

    def _write_settings(self, current, update_key):
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
                     quiz_cross_check=self.quiz_cross_check.get(),
                     quiz_review_provider=self.quiz_review_provider.get(),
                     document_seconds=int(self.document_seconds.get()),
                     playback_rate=validate_playback_rate(float(self.playback_rate.get())),
                     ai_base_url=self.ai_base_url.get().strip(), ai_model=self.ai_model.get().strip(),
                     remember_api_key=self.remember_api_key.get())
        for name in ('vision_model', 'speech_base_url', 'speech_model', 'ai_review_model'):
            saved[name] = getattr(self, name).get().strip()
        if update_key and self.remember_api_key.get():
            for name, unreadable in (('ai_api_key', self._unreadable_key),
                                     ('speech_api_key', self._unreadable_speech_key)):
                key = getattr(self, name).get().strip()
                if key:
                    saved[name + '_protected'] = protect_api_key(key)
                elif unreadable is not None:
                    saved[name + '_protected'] = unreadable
        elif not update_key:
            saved.update(api_fields(current))
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
        self._loaded_api_fields = api_fields(saved)
        if update_key:
            if self.ai_api_key.get().strip() or not self.remember_api_key.get():
                self._unreadable_key = None
            if self.speech_api_key.get().strip() or not self.remember_api_key.get():
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
            self.platform_hint.set('U校园 AI：进入具体课件后再继续；小测直接截图交给图片模型。'
                                   '听力另需转写服务；口语、自评及未适配分页/控件需手动处理。')
        else:
            self.platform_hint.set('粘贴章节链接即可自动识别视频、小测或阅读内容。若打开登录页或课程目录，请先进入章节。')

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
        self.task_table.configure(height=4)
        for widget in self.prompt_actions.winfo_children():
            widget.destroy()
        self.prompt_frame.grid_remove()
        self.prompt_report.configure(state='normal')
        self.prompt_report.delete('1.0', 'end')
        self.prompt_report.configure(state='disabled')
        self.prompt_report.pack_forget()

    def show_prompt(self, event):
        self.clear_prompt()
        self.prompt_id = event['request_id']
        task_id, request_id = self.selected_id, self.prompt_id
        self.prompt_title.configure(text=event['title'])
        self.prompt_message.configure(text=event['message'])
        if event.get('context') == 'quiz' and self.selected_job.quiz_report:
            # 暂停时为答案和操作按钮留出空间，课程仍可通过列表滚动切换。
            self.task_table.configure(height=2)
            self.prompt_report.configure(state='normal')
            self.prompt_report.insert('1.0', self.selected_job.quiz_report)
            self.prompt_report.configure(state='disabled')
            self.prompt_report.pack(fill='x', pady=(0, 8), before=self.prompt_actions)
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
        elif kind == 'quiz_report':
            job.quiz_report = event['message']
            for owner, view in getattr(self, '_ai_result_views', []):
                if owner is job and view.winfo_exists():
                    position = view.yview()[0]
                    view.configure(state='normal')
                    view.delete('1.0', 'end')
                    view.insert('1.0', job.quiz_report)
                    view.configure(state='disabled')
                    view.yview_moveto(position)
            if job is self.selected_job and job.prompt and job.prompt.get('context') == 'quiz':
                self.show_prompt(job.prompt)
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

    def show_ai_results(self):
        job = self.selected_job
        window = tk.Toplevel(self.root)
        window.title('本组 AI 返回结果' + (f' · {job.request.task_id}' if job else ''))
        window.geometry('760x480')
        content = scrolledtext.ScrolledText(window, wrap='word', font=('Microsoft YaHei UI', 10), padx=12, pady=12)
        content.pack(fill='both', expand=True)
        content.insert('1.0', job.quiz_report if job and job.quiz_report else '本课程尚未收到 AI 答案。')
        content.configure(state='disabled')
        if not hasattr(self, '_ai_result_views'):
            self._ai_result_views = []
        entry = (job, content)
        self._ai_result_views.append(entry)
        def closed(event):
            if event.widget is window and entry in self._ai_result_views:
                self._ai_result_views.remove(entry)
        window.bind('<Destroy>', closed, add='+')
        ttk.Button(window, text='关闭', command=window.destroy).pack(pady=8)
        return window

    def show_screenshot_help(self):
        messagebox.showinfo('截图答题', '每题单独截图，跨屏长题拼成整图。每批最多发 5 题，并附上共用材料。\n'
            '超星选择题可直接按网页字母作答。其他控件用截图标记定位。\n'
            '开启自动提交后会处理已识别的交卷确认，提交成功后继续下一节。\n'
            '不使用本机 OCR，也不提取题干或选项文字。\n\n'
            '请在“自动学习设置 → 截图答题”配置支持图片的模型。\n'
            '截图不完整、控件无法对应或 AI 无法确定时会暂停提示。', parent=self.root)

    def show_ocr_diagnostics(self):
        job = self.selected_job
        if job is None:
            messagebox.showinfo('识别诊断', '请先在课程任务列表中选择一门课程。', parent=self.root)
            return
        directory = task_directory(self.data_dir, job.request) / 'ocr-diagnostics'
        if not (directory / 'ocr-failure.zip').is_file():
            messagebox.showinfo('识别诊断', '本课程尚未生成诊断包。\n'
                                '本版遇到 OCR 识别失败时，会自动保存当时的文字区域图片和识别结果。',
                                parent=self.root)
            return
        try:
            os.startfile(str(directory))
        except OSError:
            messagebox.showerror('识别诊断', f'无法打开目录，请在文件资源管理器中查看：\n{directory}',
                                 parent=self.root)

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
            app.playback_rate.set('1.5')
            assert app.quiz_cross_check.get() is True
            app.quiz_cross_check.set(False)
            app.ai_review_model.set('self-test-review-vision')
            assert app.quiz_review_provider.get() == '豆包网页'
            app.quiz_review_provider.set('原 API 多轮')
            app._save_settings(update_key=True)
            app.ai_api_key.set('')
            app.speech_api_key.set('')
            app.background_playback.set(True)
            app.playback_rate.set('4')
            app.quiz_cross_check.set(True)
            app.ai_review_model.set('')
            app.quiz_review_provider.set('豆包网页')
            app._load_settings()
            assert app.background_playback.get() is False
            assert float(app.playback_rate.get()) == 1.5
            assert app.quiz_cross_check.get() is False
            assert app.ai_review_model.get() == 'self-test-review-vision'
            assert app.quiz_review_provider.get() == '原 API 多轮'
            assert app.ai_api_key.get() == 'self-test-synthetic-api-key'
            assert app.speech_api_key.get() == 'self-test-synthetic-speech-key'
            assert 'self-test-synthetic-api-key' not in (Path(directory) / 'settings.json').read_text(
                encoding='utf-8')
            assert 'self-test-synthetic-speech-key' not in (Path(directory) / 'settings.json').read_text(
                encoding='utf-8')
            import quiz_media, quiz_drag, quiz_speech
            import doubao_web
            assert doubao_web.parse_reply('{"request_id":"test","answers":[]}', 'test') == {'answers': []}
            demo = CourseJob(GuiRequest('run', 'generic', 'self-test'))
            app.jobs[demo.request.task_id] = demo
            app.task_table.insert('', 'end', iid=demo.request.task_id)
            app.select_job(demo.request.task_id)
            app.handle_event(dict(kind='quiz_report', message='原 AI：B\n豆包：A'), demo)
            app.handle_event(dict(kind='prompt', request_id=1, title='分歧', message='未提交',
                                  context='quiz', choices=[('ready', '继续')]), demo)
            assert '豆包：A' in app.prompt_report.get('1.0', 'end')
            app.handle_event(dict(kind='prompt', request_id=2, title='播放提示',
                                  message='请点击视频播放', choices=[('ready', '继续')]), demo)
            assert not app.prompt_report.frame.winfo_manager()
            assert not app.prompt_report.get('1.0', 'end').strip()
            assert '豆包：A' in demo.quiz_report
            from types import SimpleNamespace
            from quiz_screenshots import discover_groups, read_screenshots
            import quiz_dom
            from playwright.sync_api import sync_playwright
            def forbidden(*args, **kwargs):
                raise AssertionError('Screenshot mode must not call OCR/text extraction')
            quiz_dom.ocr_text = forbidden
            quiz_dom.prompt_text = forbidden
            quiz_dom.option_text = forbidden
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width':1100,'height':760})
                page.set_content('<iframe style="width:1000px;height:600px"></iframe>')
                frame = page.frames[1]
                frame.set_content('<main style="height:340px;overflow:auto"><form>'
                    '<fieldset style="height:900px"><legend>Screenshot test</legend>'
                    '<label><input type=radio name=q>Top</label><div style="height:650px"></div>'
                    '<label style="background:blue"><input type=radio name=q>Bottom</label>'
                    '</fieldset><button>Submit</button></form></main>')
                item = SimpleNamespace(root=discover_groups(frame), frame=frame)
                questions, bindings = read_screenshots(item)
                assert len(questions)==1 and len(bindings[0])==2
                assert questions[0].images[0].data.startswith(b'\x89PNG\r\n\x1a\n')
                assert len(questions[0].images[0].data)>1000
                bindings[0][1][1].click()
                assert frame.locator('input').nth(1).is_checked()
                assert frame.locator('[data-ch-capture]').count()==0
                from browser import PLAYBACK_RATE_JS, VIDEO_JS
                from core import Monitor, VideoState
                page.set_content('<video style="width:320px;height:180px"></video>')
                media = page.locator('video')
                assert media.evaluate(PLAYBACK_RATE_JS, {'key': 'test', 'rate': 2})['actual'] == 2
                assert media.evaluate(VIDEO_JS)['playback_rate'] == 2
                monitor = Monitor()
                assert monitor.step(VideoState('test', 0, 2, False, False, playback_rate=2), 0) == 'wait'
                assert monitor.step(VideoState('test', 2, 2, True, True, playback_rate=2), 1) == 'finished'
                browser.close()
            store = Store(Path(directory) / 'progress.sqlite3')
            assert store.rows() == []
            store.close()
            Path(sys.argv[2]).write_text(json.dumps({'ok': True, 'version': '1.6.48', 'tk': tk.TkVersion,
                'playwright_driver': True, 'api_key_encryption': True, 'speech_key_encryption': True,
                'multimedia_modules': True, 'screenshot_without_ocr': True,
                'scrolled_iframe_screenshot': True, 'choice_binding': True,
                'background_settings': True, 'playback_speed': True, 'cross_check_settings': True,
                'doubao_module': True, 'review_provider_settings': True, 'pause_answer_report': True,
                'video_pause_hides_quiz_report': True,
                'sqlite': True}), encoding='utf-8')
        root.destroy()
        return
    App(root, app_data_dir())
    root.mainloop()


if __name__ == '__main__':
    main()
