import json
import os.path
import pickle
import platform
import socket
import subprocess
import time
import urllib.request
from typing import Dict, List, Any
from urllib.parse import urlparse

import pyotp
from loguru import logger
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# 山东大学 aTrust 的 CAS 登录入口（可用 --portal_address 覆盖）
DEFAULT_PORTAL_ADDRESS = (
    "https://pass-sdu-edu-cn-s.atrust.sdu.edu.cn:81/cas/login"
    "?service=https%3A%2F%2Fvpn.sdu.edu.cn%3A443%2Fpassport%2Fv1%2Fauth%2Fcas"
)

# --- 登录表单输入框的模糊识别规则 ---
# placeholder / aria-label 的语义提示
_USERNAME_PLACEHOLDER_HINTS = ("用户名", "账号", "帐号", "手机号", "邮箱")
_USERNAME_PLACEHOLDER_HINTS_EN = ("user", "account", "login", "email", "mobile", "phone")
_PASSWORD_PLACEHOLDER_HINTS = ("密码",)
_PASSWORD_PLACEHOLDER_HINTS_EN = ("password", "passwd", "pwd")
# id / name 的 token（un、pd、login_un、password 之类）
_USERNAME_ID_TOKENS = {"un", "user", "username", "usr", "uname", "account", "loginname", "login", "email", "mobile"}
_PASSWORD_ID_TOKENS = {"pd", "pwd", "pass", "password", "passwd", "upass", "upwd"}
# 这些类型的 input 一律不当作账号密码框
_SKIPPED_INPUT_TYPES = {"hidden", "checkbox", "radio", "submit", "button", "reset", "file", "image"}
# 登录框常见的样式类名，仅作兜底加分
_LOGIN_INPUT_CLASSES = ("login_box_input", "login-box-input", "loginboxinput")
# 登录按钮候选选择器，按优先级排列。
# 注意：一律用 input/button/a 限定元素类型。否则像 [id*='login'] 这类写法会匹配到
# 把整块登录区包起来的 div 容器，而容器的 .text 是内部全部文字（含「登录」），
# 点它等于什么都没点。
_LOGIN_BUTTON_SELECTORS = (
    # 山大统一身份认证页
    "input#index_login_btn",
    "input.login_box_landing_btn",
    # 深信服原生登录页
    ".login-panel button",
    ".login-panel input[type='submit']",
    # 通用写法
    "input[type='submit']",
    "button[type='submit']",
    "input[type='button'][class*='login']",
    "input[class*='login_btn']",
    "input[class*='login-btn']",
    "button[class*='login_btn']",
    "button[class*='login-btn']",
    "a[class*='login_btn']",
    "a[class*='login-btn']",
)
# 兜底按文本匹配时，超过这个长度的标签一律忽略——那是容器而不是按钮
_MAX_BUTTON_LABEL_LEN = 20


class ATrustLoginStorage(BaseModel):
    cookies: List[Dict[str, Any]]
    local_storage: Dict[str, Any]

class ATrustLogin:
    def __init__(self, portal_address, driver_path=None, browser_path=None, driver_type=None, data_dir="data", cookie_tid=None, cookie_sig=None, interactive=False, container_mode=False, logged_keywords=None):
        self.initialized = False
        self.container_mode = container_mode
        if not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.interactive = interactive
        self.portal_address = portal_address
        self.portal_host = urlparse(portal_address).hostname
        self.cookie_tid = cookie_tid
        self.cookie_sig = cookie_sig

        # 登录成功后 URL fragment 里会出现的关键字。可通过 --logged_keywords 覆盖
        # （逗号分隔），以适配非深信服原生门户的登录流程。
        if isinstance(logged_keywords, str) and logged_keywords:
            self.must_be_logged_keywords = [k.strip() for k in logged_keywords.split(",") if k.strip()]
        elif logged_keywords:
            self.must_be_logged_keywords = list(logged_keywords)
        else:
            self.must_be_logged_keywords = ['app_center', 'user_info', 'app_apply', 'device_manage']
        # self.must_not_logged_keywords = ['login', 'totpAuth', 'captcha', 'page_auth_trust_terminal', 'smsAuth']

        if self.container_mode:
            from selenium.webdriver.chrome.options import Options

            DEBUG_PORT = "55555"
            PROFILE_DIR = "Default"

            binary_location = browser_path or "/usr/bin/chromium"
            chrome_data_dir = os.path.join("/tmp", "chrome-data")
            log_file = os.path.join(chrome_data_dir, "chrome.log")
            os.makedirs(chrome_data_dir, exist_ok=True)

            logger.info(f"Starting Chrome with debug port {DEBUG_PORT}")
            self.chrome_process = subprocess.Popen([
                    binary_location,
                    f"--remote-debugging-port={DEBUG_PORT}",
                    f"--user-data-dir={chrome_data_dir}",
                    f"--profile-directory={PROFILE_DIR}",
                    "--ignore-certificate-errors",
                    "--ignore-ssl-errors", 
                    "--no-sandbox", 
                    "--lang=zh-CN", 
                    "--disable-gpu", 
                    "--disable-extensions",
                    "--disable-web-security",
                    "--allow-insecure-localhost",
                    "--window-size=896,672",
                    "data:,"
                ], stdout=open(log_file, "w"), stderr=subprocess.STDOUT
            )

            logger.info(f"Chrome started, PID {self.chrome_process.pid}, waiting for DevTools ...")
            while True:
                if self.chrome_process.poll() is not None:
                    raise RuntimeError(f"Chrome exited with code {self.chrome_process.returncode}")
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version")
                    logger.info("DevTools ready.")
                    break
                except:
                    time.sleep(3)

            self.options = Options()
            self.options.debugger_address = f"127.0.0.1:{DEBUG_PORT}"
            self.driver = webdriver.Chrome(options=self.options)

        else:
            if driver_type is None:
                system = platform.system()
                if system == "Windows":
                    driver_type = "edge"
                else:
                    driver_type = "chrome"

            logger.debug(f"Driver: {driver_type}: {driver_path}")

            if driver_type == "edge":
                from selenium.webdriver.edge.options import Options
                from selenium.webdriver.edge.service import Service
                driver_cls = webdriver.Edge
            else:
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                driver_cls = webdriver.Chrome

            self.options = Options()
            self.options.add_argument('--profile-directory=ATrustLogin')
            self.options.add_argument("--ignore-certificate-errors")
            self.options.add_argument("--ignore-ssl-errors")
            self.options.add_argument("--no-sandbox")
            self.options.add_argument("--lang=zh-CN")
            self.options.add_argument("--disable-gpu")
            self.options.add_argument("--disable-extensions")
            self.options.add_argument("--disable-web-security")
            self.options.add_argument("--allow-insecure-localhost")
            self.options.add_argument("--window-size=896,672")
            self.options.add_experimental_option("prefs", {"intl.accept_languages": "zh-CN"})
            if browser_path is not None:
                self.options.binary_location = browser_path
            self.driver = driver_cls(service=Service(driver_path), options=self.options)

        self.wait = WebDriverWait(self.driver, 10)
        logger.debug("Selenium init successfully.")

    def current_cookie_domain(self):
        """返回当前页面所在的域名，供写 cookie 使用。

        cookie 的 domain 必须与当前页面域名匹配。登录页常常会跳转到 portal
        之外的域（山大那个 CAS 入口会跳到真正的统一身份认证页面），
        因此不能写死 portal 的域名，否则会抛 InvalidCookieDomainException。
        """
        try:
            host = urlparse(self.driver.current_url).hostname
        except Exception:
            host = None
        return host or self.portal_host

    # 打开登录页
    def open_portal(self):
        self.driver.get(self.portal_address)

        # 语言 cookie 只对深信服原生登录页有意义，CAS 页面用不上；
        # 且页面可能已跳转到别的域，写失败不影响主流程，故逐个容错。
        domain = self.current_cookie_domain()
        for name, value in (("language", "zh-CN"), ("lang", "zh-cn")):
            try:
                if self.driver.get_cookie(name):
                    self.driver.delete_cookie(name)
                self.driver.add_cookie(
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": "/",
                    }
                )
            except Exception as e:
                logger.debug(f"Skipped cookie {name}: {e}")

    def wait_login_page(self, timeout=30):
        """等待登录表单出现。既兼容深信服原生登录页，也兼容 CAS 等第三方登录页。"""

        def form_ready(driver):
            try:
                if driver.execute_script("return document.readyState") != "complete":
                    return False
            except Exception:
                return False

            # 深信服原生登录页
            try:
                if driver.find_elements(By.ID, "sangfor_main_auth_container") and \
                        driver.find_elements(By.CLASS_NAME, "login-panel"):
                    return True
            except Exception:
                pass

            # CAS 等第三方登录页：只要用户名和密码框都出现即认为表单就绪
            try:
                username_input, password_input = self.find_login_inputs()
                return username_input is not None and password_input is not None
            except Exception:
                return False

        try:
            WebDriverWait(self.driver, timeout).until(form_ready)
            return True
        except Exception:
            logger.warning("等待登录表单超时，继续尝试填写")
            return False

    @staticmethod
    def delay_input():
        time.sleep(0.5)

    @staticmethod
    def delay_loading():
        time.sleep(5)

    @staticmethod
    def score_input(element):
        """给一个 input 打分，返回 (用户名可能性, 密码可能性)，0 表示不像。"""
        try:
            input_type = (element.get_attribute("type") or "text").strip().lower()
        except Exception:
            input_type = "text"
        if input_type in _SKIPPED_INPUT_TYPES:
            return 0, 0

        def attr(name):
            try:
                return (element.get_attribute(name) or "").strip()
            except Exception:
                return ""

        element_id = attr("id").lower()
        name = attr("name").lower()
        classes = attr("class").lower()
        # placeholder 缺失时退而用 aria-label / title
        placeholder = attr("placeholder") or attr("aria-label") or attr("title")
        placeholder_lower = placeholder.lower()

        username_score = 0
        password_score = 0

        # 1) placeholder 的语义最明确，权重最高
        if any(hint in placeholder for hint in _USERNAME_PLACEHOLDER_HINTS) or \
                any(hint in placeholder_lower for hint in _USERNAME_PLACEHOLDER_HINTS_EN):
            username_score += 10
        if any(hint in placeholder for hint in _PASSWORD_PLACEHOLDER_HINTS) or \
                any(hint in placeholder_lower for hint in _PASSWORD_PLACEHOLDER_HINTS_EN):
            password_score += 10

        # 2) type=password 是密码框的强特征
        if input_type == "password":
            password_score += 20

        # 3) id / name 的 token 匹配，兼容 un、pd、login_un、userName 等写法
        for token in (element_id, name):
            if not token:
                continue
            parts = {token} | set(token.replace("-", "_").split("_"))
            if parts & _USERNAME_ID_TOKENS:
                username_score += 8
            if parts & _PASSWORD_ID_TOKENS:
                password_score += 8

        # 4) 登录框常见的样式类名（如 login_box_input），仅作兜底加分
        if any(cls in classes for cls in _LOGIN_INPUT_CLASSES):
            username_score += 3
            password_score += 3

        # 5) tabindex 顺序（如用户名=1、密码=2）只作弱提示，避免误判其它表单
        tabindex = attr("tabindex")
        if tabindex == "1":
            username_score += 2
        elif tabindex == "2":
            password_score += 2

        return username_score, password_score

    def find_login_inputs(self, root=None):
        """模糊查找登录表单里的用户名和密码输入框。

        不依赖固定的 id/class，而是对页面上每个 input 打分后取最优组合，
        因此页面改版或换成 CAS 登录页时通常仍然可用。
        返回 (用户名元素, 密码元素)，找不到的为 None。
        """
        root = self.driver if root is None else root
        try:
            candidates = root.find_elements(By.TAG_NAME, "input")
        except Exception:
            return None, None

        scored = []
        for index, element in enumerate(candidates):
            username_score, password_score = self.score_input(element)
            if username_score or password_score:
                scored.append({
                    "index": index,
                    "element": element,
                    "username": username_score,
                    "password": password_score,
                })

        if not scored:
            return None, None

        best_username = max(scored, key=lambda item: item["username"])
        username_input = best_username["element"] if best_username["username"] > 0 else None

        # 密码框从「除用户名框以外」的元素里挑，避免两者撞到同一个 input
        password_pool = scored
        if username_input is not None:
            password_pool = [item for item in scored if item["index"] != best_username["index"]]
        best_password = max(password_pool, key=lambda item: item["password"], default=None)
        password_input = best_password["element"] if best_password and best_password["password"] > 0 else None

        if username_input is not None:
            logger.debug(f"Matched username input: id={username_input.get_attribute('id')}, "
                         f"score={best_username['username']}")
        if password_input is not None:
            logger.debug(f"Matched password input: id={password_input.get_attribute('id')}, "
                         f"score={best_password['password']}")

        return username_input, password_input

    # 输入用户名和密码
    def enter_credentials(self, username, password):
        # 部分 aTrust 页面需要先切到「本地密码」标签
        try:
            element = self.driver.find_element(By.XPATH, "//div[contains(@class, 'server-name') and contains(text(), '本地密码')]")
            if element.is_displayed():
                self.delay_input()
                self.scroll_and_click(element)
        except Exception:
            pass

        # 在整个页面上模糊查找用户名和密码框（不再限定 sangfor_main_auth_container）
        username_input, password_input = self.find_login_inputs()

        if username_input is None or password_input is None:
            logger.info("未找到用户名或密码输入框")
            return False

        self.scroll_and_click(self.wait.until(EC.element_to_be_clickable(username_input)))
        self.delay_input()
        username_input.clear()
        username_input.send_keys(username)

        self.scroll_and_click(self.wait.until(EC.element_to_be_clickable(password_input)))
        self.delay_input()
        password_input.clear()
        password_input.send_keys(password)

        # 「记住我」一类的复选框，不是每个登录页都有
        try:
            checkbox = self.driver.find_element(By.XPATH, "//input[@type='checkbox']")
            if checkbox.is_displayed() and not checkbox.is_selected():
                self.delay_input()
                self.scroll_and_click(checkbox)  # 如果没有选中，就点击选中
        except Exception:
            pass

        logger.debug("Filled username and password")
        return True

    @staticmethod
    def is_visible_and_enabled(element):
        try:
            return element.is_displayed() and element.is_enabled()
        except Exception:
            return False

    # 查找并点击登录按钮
    def click_login_button(self):
        # 1) 按选择器逐个尝试，顺序即优先级
        for selector in _LOGIN_BUTTON_SELECTORS:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                if self.is_visible_and_enabled(element):
                    self.scroll_and_click(element)
                    logger.debug(f"Clicked login button via selector: {selector}")
                    return True

        # 2) 兜底：只在真正的交互元素里按短标签找。
        #    限制标签长度是关键——登录区若有「账号登录 / 短信登录」之类的页签，
        #    它们的文本也含「登录」，短标签约束可以避开；容器的长文本则直接跳过。
        try:
            candidates = self.driver.find_elements(
                By.CSS_SELECTOR, "input[type='submit'], input[type='button'], button, a")
        except Exception:
            candidates = []

        for element in candidates:
            if not self.is_visible_and_enabled(element):
                continue
            try:
                label = f"{element.text} {element.get_attribute('value') or ''}".strip()
            except Exception:
                continue
            if not label or len(label) > _MAX_BUTTON_LABEL_LEN:
                continue
            lowered = label.lower()
            if "登录" in label or "login" in lowered or "sign in" in lowered:
                self.scroll_and_click(element)
                logger.debug(f"Clicked login button by label: {label}")
                return True

        logger.info("未找到符合条件的登录按钮")
        return False

    def load_storage(self):
        # 从pickle文件中加载存储的数据
        try:
            if os.path.exists(os.path.join(self.data_dir, "ATrustLoginStorage.pkl")):
                with open(os.path.join(self.data_dir, "ATrustLoginStorage.pkl"), "rb") as f:
                    data = pickle.load(f)
                    # 从cookies中加载cookie
                    # 存下来的 cookie 属于当时的域，若登录页跳转到了别的域就写不进去，
                    # 这里逐个容错，避免一个陈旧 cookie 让整个流程崩掉。
                    for cookie in data.cookies:
                        try:
                            self.driver.delete_cookie(cookie['name'])
                            self.driver.add_cookie(cookie)
                        except Exception as e:
                            logger.debug(
                                f"Skipped stored cookie {cookie.get('name')} "
                                f"(cookie domain={cookie.get('domain')}, "
                                f"current url={self.driver.current_url}): {e}")
                    # 从local_storage中加载local storage
                    # 键值必须作为参数传给脚本，不能拼进 JS 字符串字面量里：
                    # 值只要含单引号、换行或反斜杠，拼出来就是非法 JS。
                    for key, value in data.local_storage.items():
                        try:
                            self.driver.execute_script(
                                "window.localStorage.setItem(arguments[0], arguments[1]);",
                                key, value)
                        except Exception as e:
                            logger.debug(f"Skipped localStorage {key}: {e}")
                    logger.info("Loaded storage data")
        except FileNotFoundError:
            logger.info("未找到存储的数据")
        except Exception as e:
            # 存储文件损坏等情况下忽略即可，重新登录一次就好
            logger.warning(f"读取存储的数据失败，已忽略：{e}")

        self.set_cli_cookie(force=False)

    def scroll_to(self, element):
        self.driver.execute_script("arguments[0].scrollIntoView();", element)

    def scroll_and_click(self, element):
        self.driver.execute_script("arguments[0].scrollIntoView();", element)
        try:
            element.click()
        except Exception as e:
            # 元素被子元素/浮层遮挡时原生 click 会失败，退回 JS click
            logger.debug(f"Native click failed ({e}), falling back to JS click")
            self.driver.execute_script("arguments[0].click();", element)
        return element

    def set_cli_cookie(self, force=False):
        """写入 --cookie_tid / --cookie_sig 指定的 cookie。

        这两个 cookie 用于绕过深信服原生登录页的图形验证码，在 CAS 等第三方
        登录页上通常用不到。因此：未提供时直接跳过（原先会把 None 当成 cookie
        的值，新版 Chrome 会直接报错）；写入失败也只记日志，不中断流程。
        """
        if self.cookie_tid is None and self.cookie_sig is None:
            return

        domain = self.current_cookie_domain()
        for name, value in (("tid", self.cookie_tid), ("tid.sig", self.cookie_sig)):
            if value is None:
                continue
            try:
                if force or not self.driver.get_cookie(name):
                    self.driver.delete_cookie(name)
                    self.driver.add_cookie({
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": "/",
                    })
            except Exception as e:
                logger.debug(f"Skipped cookie {name}: {e}")

    def require_interact(self):
        if self.interactive:
            input("Press any key to continue")
        else:
            raise Exception("User Interact required")

    def init(self):
        if not self.initialized:
            self.open_portal()
            self.wait_login_page()
            self.delay_loading()
            self.load_storage()
            self.initialized = True

    def login(self, username, password, totp_key, **kwargs):
        self.init()

        if self.is_logged():
            logger.info("Already logged in")
            return True

        if not self.enter_credentials(username=username, password=password):
            return None
        self.delay_input()
        self.click_login_button()

        logger.info("Performed basic login action")

        self.delay_loading()
        logger.debug("Checking captcha ...")

        if "图形校验码" in self.driver.page_source:
            if 'is_retried' not in kwargs:
                self.set_cli_cookie(force=True)
                self.driver.refresh()
                self.login( username, password, totp_key, is_retried=True)
                return
            else:
                logger.warning("Need to handle captcha, press any key to continue")
                self.require_interact()

        if "TOTP" in self.driver.page_source and "二次认证" in self.driver.page_source:
            if totp_key is not None:
                totp = pyotp.TOTP(totp_key)
                totp_code = totp.now()

                logger.info(f"TOTP code: {totp_code}")
                totp_input = self.driver.find_element(By.XPATH, "//input[contains(@class, 'totp')]")

                self.scroll_and_click(self.wait.until(EC.element_to_be_clickable(totp_input)))
                self.delay_input()
                totp_input.send_keys(totp_code)

                submit_button = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
                self.wait.until(EC.element_to_be_clickable(submit_button))
                self.delay_input()
                self.scroll_and_click(submit_button)
                logger.info(f"Performed TOTP login action with code: {totp_code}")
                self.delay_loading()
            else:
                logger.info("Need to handle TOTP, press any key to continue")
                self.require_interact()

        logger.info("Performed verification code login action")

        if self.is_logged():
            logger.info("Login Success")
            self.update_storage()
            return True

    def is_logged(self):
        """
        检查是否已经登录
        :return: None if not sure, True if logged, False if not logged
        """

        if self.driver.current_url.startswith('about:'):
            return None

        url = urlparse(self.driver.current_url)
        logged = any(keyword in url.fragment for keyword in self.must_be_logged_keywords)
        if not logged:
            # 打出来方便确认登录成功后的真实 URL，据此用 --logged_keywords 调整判定
            logger.debug(f"Not logged in yet, current url: {self.driver.current_url}")
        return logged

    def close(self):
        self.driver.quit()
        if self.container_mode and hasattr(self, 'chrome_process') and self.chrome_process.poll() is None:
            self.chrome_process.terminate()
            try:
                self.chrome_process.wait(timeout=5)
            except:
                self.chrome_process.kill()
                self.chrome_process.wait()

    def __enter__(self):
        return self

    def update_storage(self):
        data = ATrustLoginStorage(
            cookies=self.driver.get_cookies(),
            local_storage=self.driver.execute_script("return window.localStorage")
        )

        # save with pickle
        with open(os.path.join(self.data_dir, "ATrustLoginStorage.pkl"), "wb") as f:
            pickle.dump(data, f)

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @staticmethod
    def wait_for_port(port, host='localhost'):
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                try:
                    s.connect((host, port))
                    logger.info(f"Detected aTrust is listening on port {port}")
                    s.close()
                    break
                except (socket.timeout, ConnectionRefusedError):
                    logger.info(f"aTrust Port {port} is not yet being listened on. Waiting for aTrust start ...")
                    ATrustLogin.delay_loading()

def main(username, password, portal_address=DEFAULT_PORTAL_ADDRESS, totp_key=None, cookie_tid=None, cookie_sig=None, keepalive=200, data_dir="./data", driver_type=None, driver_path=None, browser_path=None, interactive=False, wait_atrust=True, container_mode=False, logged_keywords=None):
    logger.info("Opening Web Browser")

    if wait_atrust:
        ATrustLogin.wait_for_port(54631)

    logger.info(f"Portal address: {portal_address}")

    # 创建ATrustLogin对象
    at = ATrustLogin(data_dir=data_dir, portal_address=portal_address, cookie_tid=cookie_tid, cookie_sig=cookie_sig, driver_type=driver_type, driver_path=driver_path, browser_path=browser_path, interactive=interactive, container_mode=container_mode, logged_keywords=logged_keywords)

    at.init()

    while True:
        try:
            if not at.is_logged():
                logger.info("Session lost. Trying to login again ...")
                at.open_portal()
                at.delay_loading()
                if at.login(username=username, password=password, totp_key=totp_key) is True:
                    at.delay_loading()
                    at.delay_loading()

            if keepalive <= 0:
                at.close()
                exit(0)
            else:
                time.sleep(keepalive)
                at.open_portal()
                at.delay_loading()
        except Exception as e:
            logger.error("An error occurred when trying to login, retrying ...")
            logger.exception(e)
            at.delay_loading()

if __name__ == "__main__":
    from fire import Fire
    Fire(main)
