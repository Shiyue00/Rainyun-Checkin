import io
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations, permutations
from typing import Optional, Dict, Any

import cv2
import ddddocr
import requests
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchFrameException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from config import (
    APP_BASE_URL,
    APP_VERSION,
    CAPTCHA_RETRY_LIMIT,
    COOKIE_FILE,
    DOWNLOAD_MAX_RETRIES,
    DOWNLOAD_RETRY_DELAY,
    DOWNLOAD_TIMEOUT,
    POINTS_TO_CNY_RATE,
)


CAPTCHA_IFRAME = (By.ID, "tcaptcha_iframe_dy")
CAPTCHA_RELOAD = (By.ID, "reload")
CAPTCHA_BG = (By.ID, "slideBg")
CAPTCHA_SUBMIT = (By.XPATH, "//div[@id='tcStatus']/div[2]/div[2]/div/div")
CAPTCHA_OP = (By.ID, "tcOperation")
CAPTCHA_INSTRUCTION = (By.XPATH, "//div[@id='instruction']//img")

# 自定义异常
class CaptchaRetryableError(Exception):
    """验证码处理过程中可重试的错误"""
    pass

class APILoginError(Exception):
    """API登录失败异常"""
    pass

class CookieLoadError(Exception):
    """Cookie加载失败异常"""
    pass

# 通知模块导入
try:
    from notify import send
    NOTIFY_AVAILABLE = True
except ImportError:
    NOTIFY_AVAILABLE = False
    def send(title, content):
        logger.warning("通知模块不可用")

# 配置专业日志系统
def setup_logger():
    """配置专业的日志系统"""
    logger = logging.getLogger('RainyunAutomation')
    logger.setLevel(logging.INFO)
    
    # 清除现有处理器
    if logger.hasHandlers():
        logger.handlers.clear()
    
    # 创建格式化器
    formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d | %(levelname)-8s | %(module)s.%(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 创建字符串处理器（用于收集所有日志）
    log_capture_string = io.StringIO()
    string_handler = logging.StreamHandler(log_capture_string)
    string_handler.setFormatter(formatter)
    logger.addHandler(string_handler)
    
    # 创建控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger, log_capture_string

# 初始化日志
logger, log_capture_string = setup_logger()

@dataclass
class RuntimeContext:
    """运行时上下文"""
    driver: WebDriver
    wait: WebDriverWait
    ocr: ddddocr.DdddOcr
    det: ddddocr.DdddOcr
    temp_dir: str
    session: Optional[requests.Session] = None
    api_cookies: Optional[Dict[str, str]] = None


def build_app_url(path: str) -> str:
    """构建完整的应用URL"""
    return f"{APP_BASE_URL}/{path.lstrip('/')}"


def temp_path(ctx: RuntimeContext, filename: str) -> str:
    """获取临时文件路径"""
    return os.path.join(ctx.temp_dir, filename)


def clear_temp_dir(temp_dir: str) -> None:
    """清理临时目录"""
    if not os.path.exists(temp_dir):
        return
    for filename in os.listdir(temp_dir):
        file_path = os.path.join(temp_dir, filename)
        if os.path.isfile(file_path) or os.path.islink(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"删除临时文件失败 {file_path}: {e}")


# ==================== API快速登录核心功能 ====================

def api_login_with_requests(user: str, pwd: str) -> Dict[str, Any]:
    """
    通过API快速登录雨云，返回登录状态和Cookie
    
    Args:
        user: 用户名
        pwd: 密码
    
    Returns:
        Dict: 包含登录结果、Cookie和session的字典
    
    Raises:
        APILoginError: API登录失败时抛出
    """
    logger.info("开始API快速登录流程")
    
    session = requests.Session()
    start_time = time.time()
    
    # 配置请求头
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://v2.rainyun.com',
        'Referer': 'https://v2.rainyun.com/',
    }
    
    # 登录数据
    login_data = {
        "field": user,
        "password": pwd
    }
    
    try:
        logger.debug(f"发送登录请求到: https://api.v2.rainyun.com/user/login")
        logger.debug(f"登录数据: {login_data}")
        
        response = session.post(
            "https://api.v2.rainyun.com/user/login",
            json=login_data,
            headers=headers,
            timeout=30
        )
        
        elapsed_time = (time.time() - start_time) * 1000
        logger.debug(f"API响应时间: {elapsed_time:.2f}ms")
        logger.debug(f"响应状态码: {response.status_code}")
        logger.debug(f"响应头: {dict(response.headers)}")
        
        if response.status_code == 200:
            response_data = response.json()
            logger.info(f"API登录成功，耗时: {elapsed_time:.2f}ms")
            
            # 获取Cookie
            cookies = session.cookies.get_dict()
            logger.info(f"获取到 {len(cookies)} 个会话Cookie")
            
            # 验证响应数据
            if 'token' in response_data or 'data' in response_data or 'access_token' in response_data:
                logger.debug("响应中包含有效凭证")
            else:
                logger.warning("响应中未找到标准凭证字段")
            
            return {
                'success': True,
                'cookies': cookies,
                'session': session,
                'response_data': response_data,
                'elapsed_ms': elapsed_time
            }
        else:
            error_msg = f"API登录失败: {response.status_code}"
            try:
                error_data = response.json()
                error_msg += f" | 错误信息: {error_data}"
            except:
                error_msg += f" | 原始响应: {response.text[:200]}"
            
            logger.error(error_msg)
            raise APILoginError(error_msg)
            
    except requests.exceptions.Timeout:
        error_msg = "API登录请求超时"
        logger.error(error_msg)
        raise APILoginError(error_msg)
    except requests.exceptions.ConnectionError as e:
        error_msg = f"API连接失败: {e}"
        logger.error(error_msg)
        raise APILoginError(error_msg)
    except Exception as e:
        error_msg = f"API登录异常: {type(e).__name__}: {e}"
        logger.error(error_msg)
        raise APILoginError(error_msg)


def load_api_cookies_to_browser(driver: WebDriver, cookies: Dict[str, str], target_url: str) -> bool:
    """
    将API获取的Cookie加载到Selenium浏览器
    
    Args:
        driver: Selenium WebDriver实例
        cookies: API获取的Cookie字典
        target_url: 目标URL
    
    Returns:
        bool: Cookie加载是否成功
    """
    logger.info("开始加载API Cookie到浏览器")
    
    try:
        # 步骤1: 访问目标网站
        logger.debug(f"访问目标URL: {target_url}")
        driver.get("about:blank")  # 先清空
        driver.get(target_url)
        time.sleep(2)
        
        # 步骤2: 清除浏览器现有Cookie
        logger.debug("清除浏览器现有Cookie")
        driver.delete_all_cookies()
        time.sleep(1)
        
        # 步骤3: 解析域名
        domain = target_url.split('/')[2]
        logger.debug(f"目标域名: {domain}")
        
        # 步骤4: 添加Cookie
        added_count = 0
        for name, value in cookies.items():
            try:
                cookie_config = {
                    'name': name,
                    'value': str(value),
                    'domain': domain,
                    'path': '/',
                }
                driver.add_cookie(cookie_config)
                added_count += 1
                logger.debug(f"添加Cookie: {name}")
            except Exception as e:
                logger.warning(f"添加Cookie {name} 失败: {e}")
                # 尝试使用更通用的domain
                try:
                    cookie_config['domain'] = '.' + domain
                    driver.add_cookie(cookie_config)
                    added_count += 1
                    logger.debug(f"使用泛域名添加Cookie: {name}")
                except:
                    pass
        
        logger.info(f"成功添加 {added_count}/{len(cookies)} 个Cookie")
        
        # 步骤5: 刷新页面使Cookie生效
        logger.debug("刷新页面使Cookie生效")
        driver.refresh()
        time.sleep(3)
        
        # 步骤6: 验证登录状态
        current_url = driver.current_url
        page_source = driver.page_source
        
        login_indicators = ['登录', 'sign in', 'login']
        is_logged_in = True
        
        for indicator in login_indicators:
            if indicator.lower() in page_source.lower():
                is_logged_in = False
                break
        
        if is_logged_in or 'dashboard' in current_url:
            logger.info("✅ Cookie加载成功，浏览器已保持登录状态")
            return True
        else:
            logger.warning("Cookie可能无效或已过期")
            return False
            
    except Exception as e:
        logger.error(f"加载Cookie到浏览器失败: {type(e).__name__}: {e}")
        return False


def hybrid_login_flow(ctx: RuntimeContext, user: str, pwd: str) -> bool:
    """
    混合登录流程：优先使用API快速登录，失败时回退到浏览器登录
    
    Args:
        ctx: 运行时上下文
        user: 用户名
        pwd: 密码
    
    Returns:
        bool: 登录是否成功
    """
    logger.info("开始混合登录流程")
    login_start_time = time.time()
    
    # 第一阶段：尝试API快速登录
    logger.info("阶段1: 尝试API快速登录")
    try:
        api_result = api_login_with_requests(user, pwd)
        
        if api_result['success']:
            logger.info("API快速登录成功，开始注入Cookie到浏览器")
            ctx.api_cookies = api_result['cookies']
            ctx.session = api_result['session']
            
            # 注入Cookie到浏览器
            if load_api_cookies_to_browser(ctx.driver, api_result['cookies'], build_app_url("/dashboard")):
                login_elapsed = (time.time() - login_start_time) * 1000
                logger.info(f"✅ 混合登录成功，总耗时: {login_elapsed:.2f}ms")
                return True
            else:
                logger.warning("Cookie注入失败，继续第二阶段")
        else:
            logger.warning("API登录未返回成功状态，继续第二阶段")
            
    except APILoginError as e:
        logger.warning(f"API登录失败: {e}，继续第二阶段")
    except Exception as e:
        logger.error(f"API登录流程异常: {type(e).__name__}: {e}，继续第二阶段")
    
    # 第二阶段：回退到浏览器登录
    logger.info("阶段2: 回退到浏览器登录")
    try:
        if do_browser_login(ctx, user, pwd):
            login_elapsed = (time.time() - login_start_time) * 1000
            logger.info(f"✅ 浏览器登录成功，总耗时: {login_elapsed:.2f}ms")
            return True
        else:
            logger.error("浏览器登录失败")
            return False
    except Exception as e:
        logger.error(f"浏览器登录异常: {type(e).__name__}: {e}")
        return False


def do_browser_login(ctx: RuntimeContext, user: str, pwd: str) -> bool:
    """
    浏览器登录流程
    
    Args:
        ctx: 运行时上下文
        user: 用户名
        pwd: 密码
    
    Returns:
        bool: 登录是否成功
    """
    logger.info("执行浏览器登录流程")
    login_url = build_app_url("/auth/login")
    
    try:
        # 访问登录页面
        logger.debug(f"访问登录页面: {login_url}")
        ctx.driver.get(login_url)
        
        # 等待并填写表单
        logger.debug("等待登录表单加载")
        username_field = ctx.wait.until(EC.visibility_of_element_located((By.NAME, 'login-field')))
        password_field = ctx.wait.until(EC.visibility_of_element_located((By.NAME, 'login-password')))
        login_button = ctx.wait.until(EC.visibility_of_element_located(
            (By.XPATH, '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button')
        ))
        
        # 填写表单
        logger.debug("填写登录表单")
        username_field.send_keys(user)
        password_field.send_keys(pwd)
        login_button.click()
        
        # 处理验证码（如果存在）
        try:
            logger.debug("检查验证码")
            ctx.driver.switch_to.default_content()
            ctx.wait.until(EC.frame_to_be_available_and_switch_to_it(CAPTCHA_IFRAME))
            logger.info("检测到验证码，开始处理")

            if not process_captcha(ctx):
                logger.error("验证码处理失败")
                return False

        except TimeoutException:
            logger.debug("未检测到验证码")
        except NoSuchFrameException:
            logger.debug("验证码iframe尚未就绪，视为未检测到验证码")
        finally:
            ctx.driver.switch_to.default_content()

        time.sleep(2)
        
        # 验证登录成功
        logger.debug("验证登录状态")
        ctx.wait.until(EC.url_contains("dashboard"))
        
        # 验证用户信息
        current_url = ctx.driver.current_url
        logger.info(f"登录成功，当前URL: {current_url}")
        
        return True
        
    except TimeoutException as e:
        logger.error(f"浏览器登录超时: {e}")
        return False
    except Exception as e:
        logger.error(f"浏览器登录异常: {type(e).__name__}: {e}")
        return False


def init_selenium(debug: bool, linux: bool) -> WebDriver:
    """初始化Selenium WebDriver"""
    logger.debug("初始化Selenium WebDriver")
    
    options = Options()
    
    # 基础配置
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    if debug:
        options.add_experimental_option("detach", True)
        logger.debug("启用调试模式")
    
    if linux:
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        logger.debug("Linux环境配置: 无头模式")
        
        # 二进制路径
        chrome_bin = os.environ.get("CHROME_BIN")
        if chrome_bin and os.path.exists(chrome_bin):
            options.binary_location = chrome_bin
            logger.debug(f"使用自定义Chrome二进制: {chrome_bin}")
        
        # Driver路径
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "/usr/local/share/chromedriver-linux64/chromedriver")
        if os.path.exists(chromedriver_path):
            logger.debug(f"使用系统chromedriver: {chromedriver_path}")
            return webdriver.Chrome(service=Service(chromedriver_path), options=options)
        else:
            logger.debug("使用本地chromedriver")
            return webdriver.Chrome(service=Service("./chromedriver"), options=options)
    else:
        logger.debug("Windows环境配置")
        return webdriver.Chrome(service=Service("chromedriver.exe"), options=options)


def download_image(url: str, output_path: str) -> bool:
    """下载图片到本地"""
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            logger.debug(f"下载图片 (尝试 {attempt}/{DOWNLOAD_MAX_RETRIES}): {url}")
            
            response = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
            response.raise_for_status()
            
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            file_size = os.path.getsize(output_path)
            logger.debug(f"图片下载成功: {output_path} ({file_size} bytes)")
            return True
            
        except requests.RequestException as e:
            logger.warning(f"下载图片失败 (尝试 {attempt}): {e}")
            if attempt < DOWNLOAD_MAX_RETRIES:
                time.sleep(DOWNLOAD_RETRY_DELAY)
    
    logger.error(f"图片下载失败，已重试 {DOWNLOAD_MAX_RETRIES} 次: {url}")
    return False


def get_url_from_style(style: str) -> str:
    """从style属性中提取URL"""
    if not style:
        raise ValueError("style属性为空")
    
    match = re.search(r"url\(([^)]+)\)", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从style中解析URL: {style}")
    
    url = match.group(1).strip().strip('"').strip("'")
    return url


def get_width_from_style(style: str) -> float:
    """从style属性中提取宽度"""
    if not style:
        raise ValueError("style属性为空")
    
    match = re.search(r"width\s*:\s*([\d.]+)px", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从style中解析宽度: {style}")
    
    return float(match.group(1))


def get_height_from_style(style: str) -> float:
    """从style属性中提取高度"""
    if not style:
        raise ValueError("style属性为空")
    
    match = re.search(r"height\s*:\s*([\d.]+)px", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从style中解析高度: {style}")
    
    return float(match.group(1))


def get_element_size(element) -> tuple[float, float]:
    """获取元素尺寸"""
    size = element.size or {}
    width = size.get("width", 0)
    height = size.get("height", 0)
    
    if not width or not height:
        raise ValueError("无法从元素尺寸解析宽高")
    
    return float(width), float(height)


@dataclass(frozen=True)
class CaptchaMatchResult:
    positions: list[tuple[int, int]]
    similarities: list[float]
    method: str


class SiftCaptchaMatcher:
    name = "sift"

    def __init__(self):
        self.sift = cv2.SIFT_create() if hasattr(cv2, "SIFT_create") else None

    def match(self, background, sprites, bboxes):
        if self.sift is None:
            return None
        return build_match_result(background, sprites, bboxes, self._similarity, self.name)

    def _similarity(self, sprite, spec):
        sprite_gray = normalize_gray(sprite)
        spec_gray = normalize_gray(spec)
        if sprite_gray is None or spec_gray is None:
            return 0.0

        try:
            _, des1 = self.sift.detectAndCompute(sprite_gray, None)
            _, des2 = self.sift.detectAndCompute(spec_gray, None)
            if des1 is None or des2 is None:
                return 0.0
            matches = cv2.BFMatcher().knnMatch(des1, des2, k=2)
        except cv2.error:
            return 0.0

        good = [m for pair in matches if len(pair) == 2 for m, n in [pair] if m.distance < 0.8 * n.distance]
        return len(good) / len(matches) if matches else 0.0


class TemplateCaptchaMatcher:
    name = "template"

    def match(self, background, sprites, bboxes):
        return build_match_result(background, sprites, bboxes, self._similarity, self.name)

    @staticmethod
    def _similarity(sprite, spec):
        sprite_gray = normalize_gray(sprite)
        spec_gray = normalize_gray(spec)
        if sprite_gray is None or spec_gray is None:
            return 0.0
        try:
            sprite_gray = cv2.resize(sprite_gray, (spec_gray.shape[1], spec_gray.shape[0]))
            result = cv2.matchTemplate(spec_gray, sprite_gray, cv2.TM_CCOEFF_NORMED)
            return float(result.max())
        except cv2.error:
            return 0.0


class StrategyCaptchaSolver:
    def __init__(self):
        self.matchers = [SiftCaptchaMatcher(), TemplateCaptchaMatcher()]

    def solve(self, background, sprites, bboxes):
        for matcher in self.matchers:
            result = matcher.match(background, sprites, bboxes)
            if result:
                logger.info(f"验证码匹配策略命中: {matcher.name}")
                return result
            logger.warning(f"验证码匹配策略失败: {matcher.name}")
        return None


def normalize_gray(image):
    if image is None or getattr(image, "size", 0) == 0:
        return None
    try:
        if len(image.shape) == 3:
            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        return image
    except cv2.error:
        return None


def build_match_result(background, sprites, bboxes, similarity_fn, method):
    if background is None or len(sprites) != 3 or not bboxes:
        return None

    height, width = background.shape[:2]
    valid_specs = []
    for bbox in bboxes:
        if len(bbox) != 4:
            continue
        x1, y1, x2, y2 = map(int, bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        spec = background[y1:y2, x1:x2]
        if getattr(spec, "size", 0) == 0:
            continue
        valid_specs.append(((int((x1 + x2) / 2), int((y1 + y2) / 2)), spec))

    if len(valid_specs) < len(sprites):
        return None

    sim_matrix = []
    for sprite in sprites:
        sim_matrix.append([similarity_fn(sprite, spec) for _, spec in valid_specs])

    best_perm = None
    best_scores = None
    best_key = None
    bbox_indices = range(len(valid_specs))
    for chosen in combinations(bbox_indices, len(sprites)):
        for perm in permutations(chosen):
            scores = [sim_matrix[i][perm[i]] for i in range(len(sprites))]
            key = (min(scores), sum(scores) / len(scores), sum(scores))
            if best_key is None or key > best_key:
                best_key = key
                best_perm = perm
                best_scores = scores

    if best_perm is None or best_scores is None:
        return None

    positions = [valid_specs[index][0] for index in best_perm]
    return CaptchaMatchResult(positions, [float(score) for score in best_scores], method)


def _switch_to_captcha_frame(ctx: RuntimeContext, timeout: float | None = None) -> bool:
    """重新定位并切入验证码 iframe，兼容 iframe 重渲染。"""
    try:
        ctx.driver.switch_to.default_content()
        wait_seconds = timeout if timeout is not None else max(3, min(8, getattr(ctx.wait, "_timeout", 8)))
        return bool(
            WebDriverWait(ctx.driver, wait_seconds, poll_frequency=0.25).until(
                EC.frame_to_be_available_and_switch_to_it(CAPTCHA_IFRAME)
            )
        )
    except (TimeoutException, NoSuchFrameException, WebDriverException):
        ctx.driver.switch_to.default_content()
        return False


def refresh_captcha(ctx: RuntimeContext) -> bool:
    """等待验证码刷新按钮可交互，必要时使用 JS 兜底点击。"""
    try:
        if not _switch_to_captcha_frame(ctx):
            logger.error("无法重新进入验证码 iframe，可能验证码已经关闭")
            return False

        wait_seconds = max(5, min(10, getattr(ctx.wait, "_timeout", 10)))
        wait = WebDriverWait(ctx.driver, wait_seconds, poll_frequency=0.25)
        try:
            reload_btn = wait.until(EC.element_to_be_clickable(CAPTCHA_RELOAD))
            ctx.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});", reload_btn
            )
            try:
                reload_btn.click()
            except (
                ElementNotInteractableException,
                ElementClickInterceptedException,
                StaleElementReferenceException,
                WebDriverException,
            ):
                reload_btn = wait.until(EC.presence_of_element_located(CAPTCHA_RELOAD))
                ctx.driver.execute_script("arguments[0].click();", reload_btn)
        except TimeoutException:
            # 某些版本在刷新动画期间一直报告不可点击，但按钮仍有可用的 DOM 事件。
            reload_btn = wait.until(EC.presence_of_element_located(CAPTCHA_RELOAD))
            ctx.driver.execute_script("arguments[0].click();", reload_btn)

        wait.until(EC.visibility_of_element_located(CAPTCHA_BG))
        return True
    except (TimeoutException, NoSuchFrameException, WebDriverException) as refresh_error:
        logger.error(f"无法刷新验证码: {type(refresh_error).__name__}: {refresh_error}")
        ctx.driver.switch_to.default_content()
        return False


def wait_captcha_result(ctx: RuntimeContext) -> bool | None:
    """等待验证码结果；若 iframe 已关闭则检查父页面状态。"""
    wait_seconds = max(5, getattr(ctx.wait, "_timeout", 15))

    def probe(driver):
        try:
            result_el = driver.find_element(*CAPTCHA_OP)
            classes = result_el.get_attribute("class") or ""
            if "show-success" in classes:
                return True
            if any(marker in classes for marker in ("show-fail", "show-error", "show-failure")):
                return False
        except WebDriverException:
            return None
        return None

    try:
        return WebDriverWait(ctx.driver, wait_seconds, poll_frequency=0.25).until(probe)
    except TimeoutException:
        # 提交成功时腾讯验证码 iframe 可能会被移除，不能再尝试点击 iframe 内的刷新按钮。
        ctx.driver.switch_to.default_content()
        try:
            if "dashboard" in (ctx.driver.current_url or ""):
                logger.info("验证码 iframe 已关闭，检测到登录页已跳转")
                return True
            iframe_exists = bool(ctx.driver.find_elements(*CAPTCHA_IFRAME))
            page_source = ctx.driver.page_source or ""
            done_markers = ("已领取", "已完成", "已签到", "明日再来")
            if not iframe_exists and any(marker in page_source for marker in done_markers):
                logger.info("验证码 iframe 已关闭，检测到签到完成状态")
                return True
        except WebDriverException:
            pass
        return None


def detect_captcha_bboxes(ctx: RuntimeContext, captcha_bytes: bytes, captcha_image):
    """优先使用原始图片检测，失败时再使用重新编码的图片检测。"""
    payloads = [captcha_bytes]
    try:
        ok, encoded = cv2.imencode(".jpg", captcha_image)
        if ok:
            payloads.append(encoded.tobytes())
    except cv2.error:
        pass

    for payload in payloads:
        try:
            bboxes = ctx.det.detection(payload)
            if bboxes:
                return bboxes
        except Exception as detection_error:
            logger.warning(f"验证码目标检测失败: {detection_error}")
    return []


def process_captcha(ctx: RuntimeContext, retry_count: int = 0) -> bool:
    """使用循环重试和 SIFT/模板双策略处理验证码。"""
    solver = StrategyCaptchaSolver()
    current_retry = retry_count

    while current_retry < CAPTCHA_RETRY_LIMIT:
        logger.info(f"开始处理验证码 (尝试 {current_retry + 1}/{CAPTCHA_RETRY_LIMIT})")
        try:
            download_captcha_img(ctx)
            captcha = cv2.imread(temp_path(ctx, "captcha.jpg"))
            if captcha is None:
                raise CaptchaRetryableError("验证码背景图读取失败")

            if not check_captcha(ctx):
                logger.error("当前验证码识别率低，尝试刷新")
            else:
                with open(temp_path(ctx, "captcha.jpg"), "rb") as captcha_file:
                    captcha_bytes = captcha_file.read()
                bboxes = detect_captcha_bboxes(ctx, captcha_bytes, captcha)
                result = solver.solve(
                    captcha,
                    [cv2.imread(temp_path(ctx, f"sprite_{index}.jpg")) for index in range(1, 4)],
                    bboxes,
                )

                if result and check_answer(result):
                    for index, (position, similarity) in enumerate(
                        zip(result.positions, result.similarities), start=1
                    ):
                        logger.info(
                            f"图案 {index} 位于 ({position[0]},{position[1]})，匹配率: {similarity:.4f}，策略: {result.method}"
                        )
                        slide_bg = ctx.wait.until(EC.visibility_of_element_located(CAPTCHA_BG))
                        style = slide_bg.get_attribute("style")
                        width_raw, height_raw = captcha.shape[1], captcha.shape[0]
                        try:
                            width = get_width_from_style(style)
                            height = get_height_from_style(style)
                        except ValueError:
                            width, height = get_element_size(slide_bg)

                        x, y = position
                        final_x = int(-width / 2 + x / width_raw * width)
                        final_y = int(-height / 2 + y / height_raw * height)
                        ActionChains(ctx.driver).move_to_element_with_offset(
                            slide_bg, final_x, final_y
                        ).click().perform()

                    confirm = ctx.wait.until(EC.element_to_be_clickable(CAPTCHA_SUBMIT))
                    logger.info("提交验证码")
                    confirm.click()
                    result = wait_captcha_result(ctx)
                    if result is True:
                        logger.info("验证码通过")
                        return True
                    if result is False:
                        logger.error("验证码未通过，尝试刷新")
                    else:
                        logger.error("验证码结果超时，尝试重新进入 iframe 后刷新")
                else:
                    logger.error("验证码匹配失败或结果不完整，尝试刷新")
        except (TimeoutException, ValueError, CaptchaRetryableError, WebDriverException) as error:
            logger.error(f"验证码处理异常: {type(error).__name__}: {error}")

        current_retry += 1
        if current_retry >= CAPTCHA_RETRY_LIMIT:
            break
        if not refresh_captcha(ctx):
            return False

    logger.error(f"验证码重试次数过多 ({CAPTCHA_RETRY_LIMIT} 次)，任务失败")
    return False


def download_captcha_img(ctx: RuntimeContext):
    """下载当前验证码背景图和小图。"""
    clear_temp_dir(ctx.temp_dir)
    slide_bg = ctx.wait.until(EC.visibility_of_element_located(CAPTCHA_BG))
    img1_url = get_url_from_style(slide_bg.get_attribute("style"))
    logger.info(f"下载验证码背景图: {img1_url}")
    if not download_image(img1_url, temp_path(ctx, "captcha.jpg")):
        raise CaptchaRetryableError("验证码背景图下载失败")

    sprite = ctx.wait.until(EC.visibility_of_element_located(CAPTCHA_INSTRUCTION))
    img2_url = sprite.get_attribute("src")
    logger.info(f"下载验证码小图: {img2_url}")
    if not img2_url or not download_image(img2_url, temp_path(ctx, "sprite.jpg")):
        raise CaptchaRetryableError("验证码小图下载失败")


def check_captcha(ctx: RuntimeContext) -> bool:
    """检查验证码小图是否为有效素材，并切分成三个目标图案。"""
    raw = cv2.imread(temp_path(ctx, "sprite.jpg"))
    if raw is None or raw.size == 0:
        logger.error("验证码小图读取失败")
        return False

    width = raw.shape[1]
    split_points = (0, width // 3, (width * 2) // 3, width)
    for index in range(3):
        sprite = raw[:, split_points[index]:split_points[index + 1]]
        if sprite.size == 0:
            return False
        output_path = temp_path(ctx, f"sprite_{index + 1}.jpg")
        cv2.imwrite(output_path, sprite)
        with open(output_path, "rb") as sprite_file:
            if ctx.ocr.classification(sprite_file.read()) in ["0", "1"]:
                return False
    return True


def check_answer(result: CaptchaMatchResult | None) -> bool:
    """确保三个目标图案对应三个不同的候选位置。"""
    if not result or len(result.positions) != 3:
        return False
    return len(set(result.positions)) == 3


def compute_similarity(img1_path, img2_path):
    """保留旧接口，供外部调用时返回 SIFT 相似度。"""
    img1 = cv2.imread(img1_path)
    img2 = cv2.imread(img2_path)
    matcher = SiftCaptchaMatcher()
    if matcher.sift is None:
        return 0.0, 0
    score = matcher._similarity(img1, img2)
    return score, 0


def run():
    """主运行函数"""
    ctx = None
    driver = None
    temp_dir = None
    execution_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    try:
        # 执行ID
        logger.info(f"执行ID: {execution_id}")
        logger.info(f"━━━━━━ 雨云自动化签到 v{APP_VERSION} ━━━━━━")
        
        # 读取配置
        timeout = int(os.environ.get("TIMEOUT", "15"))
        max_delay = int(os.environ.get("MAX_DELAY", "90"))
        user = os.environ.get("RAINYUN_USER", "")
        pwd = os.environ.get("RAINYUN_PWD", "")
        debug = True
        linux = True
        
        # 验证配置
        if not user or not pwd:
            logger.critical("环境变量 RAINYUN_USER 和 RAINYUN_PWD 未设置")
            raise ValueError("缺少必要的登录凭据")
        
        logger.info(f"用户名: {user}")
        logger.info(f"执行环境: {'Linux' if linux else 'Windows'}")
        logger.info(f"超时设置: {timeout}s")
        
        # 随机延迟
        # delay = random.randint(0, max_delay)
        # delay_sec = random.randint(0, 60)
        # logger.debug(f"随机延迟: {delay}m {delay_sec}s")
        # time.sleep(delay * 60 + delay_sec)
        
        # 初始化组件
        logger.info("初始化组件")
        
        logger.debug("初始化ddddocr")
        ocr = ddddocr.DdddOcr(ocr=True, show_ad=False)
        det = ddddocr.DdddOcr(det=True, show_ad=False)
        
        logger.debug("初始化Selenium WebDriver")
        driver = init_selenium(debug=debug, linux=linux)
        
        # 反检测
        logger.debug("配置反检测")
        with open("stealth.min.js", mode="r") as f:
            js = f.read()
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})
        
        wait = WebDriverWait(driver, timeout)
        temp_dir = tempfile.mkdtemp(prefix=f"rainyun_{execution_id}_")
        
        ctx = RuntimeContext(
            driver=driver,
            wait=wait,
            ocr=ocr,
            det=det,
            temp_dir=temp_dir
        )
        
        logger.info("━━━━━━ 开始登录流程 ━━━━━━")
        
        # 使用混合登录流程
        logged_in = hybrid_login_flow(ctx, user, pwd)
        if not logged_in:
            logger.error("登录流程失败，任务终止")
            raise Exception("登录失败")
        
        logger.info("━━━━━━ 开始签到流程 ━━━━━━")
        
        # 跳转到赚取积分页面
        earn_url = build_app_url("/account/reward/earn")
        logger.info(f"访问赚取积分页面: {earn_url}")
        ctx.driver.get(earn_url)
        time.sleep(3)
        
        # 检查签到状态
        try:
            earn_button = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//span[contains(text(), '每日签到')]/ancestor::div[1]//a[contains(text(), '领取奖励')]")
            ))
            logger.info("找到签到按钮，开始签到")
            earn_button.click()
        except TimeoutException:
            # 检查是否已签到
            already_signed_patterns = ['已领取', '已完成', '已签到', '明日再来']
            page_source = ctx.driver.page_source
            
            for pattern in already_signed_patterns:
                if pattern in page_source:
                    logger.info(f"今日已签到 (检测到: '{pattern}')")
                    logger.info("跳过签到流程，获取积分信息")
                    
                    try:
                        points_element = wait.until(EC.visibility_of_element_located(
                            (By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3')
                        ))
                        points_text = points_element.get_attribute("textContent")
                        current_points = int(''.join(re.findall(r'\d+', points_text)))
                        cny_value = current_points / POINTS_TO_CNY_RATE
                        logger.info(f"当前积分: {current_points:,} | 约 ¥{cny_value:.2f} 元")
                    except Exception as e:
                        logger.warning(f"获取积分信息失败: {e}")
                    
                    return
        
        # 处理签到验证码
        logger.info("处理签到验证码")
        try:
            ctx.driver.switch_to.default_content()
            ctx.wait.until(EC.frame_to_be_available_and_switch_to_it(CAPTCHA_IFRAME))
            if not process_captcha(ctx):
                logger.error("验证码处理失败")
                raise Exception("签到验证码失败")
        except (TimeoutException, NoSuchFrameException):
            logger.warning("未找到验证码iframe，可能不需要验证码")
        finally:
            ctx.driver.switch_to.default_content()
        
        # 等待签到完成
        time.sleep(3)
        
        # 获取积分信息
        try:
            points_element = wait.until(EC.visibility_of_element_located(
                (By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3')
            ))
            points_text = points_element.get_attribute("textContent")
            current_points = int(''.join(re.findall(r'\d+', points_text)))
            cny_value = current_points / POINTS_TO_CNY_RATE
            
            logger.info(f"签到成功！当前积分: {current_points:,} | 约 ¥{cny_value:.2f} 元")
            logger.info("━━━━━━ 任务执行完成 ━━━━━━")
            
        except Exception as e:
            logger.warning(f"获取积分信息失败: {e}")
            logger.info("任务执行完成")
        
    except Exception as e:
        logger.critical(f"任务执行失败: {type(e).__name__}: {e}")
        
    finally:
        # 清理资源
        logger.info("开始清理资源")
        
        if driver:
            try:
                logger.debug("关闭WebDriver")
                driver.quit()
            except Exception as e:
                logger.warning(f"关闭WebDriver失败: {e}")
        
        if temp_dir and not debug:
            try:
                logger.debug(f"清理临时目录: {temp_dir}")
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"清理临时目录失败: {e}")
        
        # 发送通知
        if NOTIFY_AVAILABLE:
            try:
                log_content = log_capture_string.getvalue()
                send(f"雨云签到 - {execution_id}", log_content)
                logger.info("通知发送完成")
            except Exception as e:
                logger.warning(f"发送通知失败: {e}")
        
        # 关闭日志收集器
        log_capture_string.close()


if __name__ == "__main__":
    run()
