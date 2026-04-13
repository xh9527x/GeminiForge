# -*- coding: utf-8 -*-
"""
GeminiForge (原 gtgm) - Gemini Business 账号注册机
专为GitHub Actions无头环境设计，使用Playwright替代DrissionPage
"""

import os
import sys
import json
import re
import time
import random
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from playwright_stealth import Stealth  # 核心修正：导入最新的 Stealth 类

# 配置日志双写：强制物理落盘 + 终端输出
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    filename='registration.log',
    filemode='a'
)
console = logging.StreamHandler()
console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger('').addHandler(console)
logger = logging.getLogger(__name__)

PROXY = os.environ.get('PROXY', '')

@dataclass
class CredentialData:
    email: str = ""
    csesidx: str = ""
    config_id: str = ""
    c_ses: str = ""
    c_oses: str = ""
    
    def to_dict(self) -> dict:
        expires_at = (datetime.now() + timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "id": self.email,
            "csesidx": self.csesidx,
            "config_id": self.config_id,
            "secure_c_ses": self.c_ses,
            "host_c_oses": self.c_oses,
            "expires_at": expires_at
        }
    
    def is_complete(self) -> bool:
        return all([self.csesidx, self.config_id, self.c_ses, self.c_oses])

class EmailManager:
    def __init__(self, worker_domain: str, email_domain: str, admin_password: str):
        self.worker_domain = re.sub(r'^https?://', '', worker_domain).rstrip('/')
        self.email_domain = email_domain
        self.admin_password = admin_password
        
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=5, pool_maxsize=5)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        self.session.headers.update({'Connection': 'keep-alive'})
    
    def _update_proxy(self):
        use_proxy_for_email = os.environ.get('PROXY_EMAIL', '').lower() == 'true'
        if not use_proxy_for_email:
            return  
        
        proxy = os.environ.get('PROXY', '') or PROXY
        if proxy and not self.session.proxies:
            self.session.proxies = {'http': proxy, 'https': proxy}
            logger.info(f"EmailManager 使用代理: {proxy[:30]}...")
    
    def create_email(self, max_retries: int = 3) -> tuple:
        import string
        self._update_proxy()
        
        letters1 = ''.join(random.choices(string.ascii_lowercase, k=4))
        numbers = ''.join(random.choices(string.digits, k=2))
        letters2 = ''.join(random.choices(string.ascii_lowercase, k=3))
        username = letters1 + numbers + letters2
        
        url = f"https://{self.worker_domain}/admin/new_address"
        headers = {"Content-Type": "application/json", "x-admin-auth": self.admin_password}
        payload = {"enablePrefix": True, "name": username, "domain": self.email_domain}
        
        for attempt in range(max_retries):
            try:
                res = self.session.post(url, json=payload, headers=headers, timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    email = data.get('address', f"{username}@{self.email_domain}")
                    logger.info(f"邮箱创建成功: {email}")
                    return data.get('jwt', ''), email
            except Exception as e:
                wait_time = (2 ** attempt) + 1
                logger.warning(f"创建邮箱失败 ({attempt + 1}/{max_retries}): {e}")
                time.sleep(wait_time)
        
        return None, None
    
    def check_verification_code(self, email: str, max_retries: int = 20) -> Optional[str]:
        self._update_proxy()
        
        for i in range(max_retries):
            try:
                url = f"https://{self.worker_domain}/admin/mails"
                headers = {"x-admin-auth": self.admin_password}
                params = {"limit": 5, "offset": 0, "address": email}
                
                res = self.session.get(url, params=params, headers=headers, timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    if data.get('results') and len(data['results']) > 0:
                        raw_content = data['results'][0].get('raw', '')
                        cleaned = raw_content.replace('=\r\n', '').replace('=\n', '').replace('=3D', '=')
                        
                        patterns = [
                            r'verification-code[^>]*>([A-Z0-9]{6})<',
                            r'>([A-Z0-9]{6})</span>',
                            r'\b([A-Z0-9]{6})\b',
                        ]
                        for pattern in patterns:
                            match = re.search(pattern, cleaned, re.IGNORECASE)
                            if match:
                                code = match.group(1).upper()
                                if len(code) == 6 and code.isalnum():
                                    logger.info(f"获取到验证码: {code}")
                                    return code
                
                logger.info(f"等待验证码... ({i+1}/{max_retries})")
                time.sleep(3)
            except Exception as e:
                logger.warning(f"检查验证码错误: {e}")
                time.sleep(3)
        
        return None

class GeminiRegistrar:
    def __init__(self, email_config: Dict):
        self.email_config = email_config
        self.credential = CredentialData()
        self.email_manager = EmailManager(
            email_config['worker_domain'],
            email_config['email_domain'],
            email_config['admin_password']
        )
        self.browser = None
        self.page = None
    
    async def register(self) -> bool:
        from playwright.async_api import async_playwright
        
        try:
            logger.info("正在创建邮箱...")
            jwt, email = self.email_manager.create_email()
            if not email:
                raise Exception("创建邮箱失败")
            self.credential.email = email
            
            logger.info("正在启动浏览器...")
            async with async_playwright() as p:
                launch_args = {
                    'headless': True,
                    'args': [
                        '--disable-blink-features=AutomationControlled',
                        '--disable-infobars',
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--window-size=1920,1080'
                    ]
                }
                
                browser_proxy = os.environ.get('PROXY', '') or PROXY
                if browser_proxy:
                    from urllib.parse import urlparse
                    proxy_parsed = urlparse(browser_proxy)
                    proxy_config = {'server': f"http://{proxy_parsed.hostname}:{proxy_parsed.port}"}
                    if proxy_parsed.username:
                        proxy_config['username'] = proxy_parsed.username
                        proxy_config['password'] = proxy_parsed.password or ''
                    launch_args['proxy'] = proxy_config
                    logger.info(f"浏览器使用代理: {proxy_parsed.hostname}:{proxy_parsed.port}")
                
                self.browser = await p.chromium.launch(**launch_args)
                context = await self.browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
                    viewport={'width': 1920, 'height': 1080},
                    java_script_enabled=True,
                    bypass_csp=True
                )
                
                # 核心修正：适配最新 playwright-stealth v2.0+ API
                # 将隐匿特征直接挂载到 Context 级，抹除所有衍生 Page 的指纹
                stealth = Stealth()
                await stealth.apply_stealth_async(context)
                
                self.page = await context.new_page()
                
                logger.info("正在打开注册页面...")
                await self.page.goto('https://business.gemini.com', wait_until='networkidle')
                
                logger.info(f"正在输入邮箱: {email}")
                await self.page.wait_for_selector('#email-input', timeout=30000)
                
                # 拟人化交互
                await self.page.locator('#email-input').click()
                await self.page.locator('#email-input').type(email, delay=120)
                await asyncio.sleep(2)
                
                await self.page.click('#log-in-button')
                logger.info("已点击登录按钮，等待目标站点响应...")
                await asyncio.sleep(6)
                
                debug_img = f"debug_screenshot_{email.split('@')[0]}.png"
                await self.page.screenshot(path=debug_img, full_page=True)
                logger.info(f"⚠️ 交互快照已覆盖保存: {debug_img}")
                
                logger.info("正在等待验证码...")
                code = self.email_manager.check_verification_code(email)
                if not code:
                    raise Exception("未收到验证码，请检查 Artifacts 截图是否依旧被 Google 拦截")
                
                logger.info(f"正在输入验证码: {code}")
                await self.page.wait_for_selector('input[name="pinInput"]', timeout=30000)
                await self.page.locator('input[name="pinInput"]').click()
                await self.page.locator('input[name="pinInput"]').type(code, delay=100)
                await asyncio.sleep(1)
                
                await self.page.click('button[jsname="XooR8e"]')
                await asyncio.sleep(3)
                
                logger.info("正在输入姓名...")
                await self.page.wait_for_selector('input[formcontrolname="fullName"]', timeout=15000)
                fullname = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=5))
                await self.page.locator('input[formcontrolname="fullName"]').click()
                await self.page.locator('input[formcontrolname="fullName"]').type(fullname, delay=100)
                await asyncio.sleep(1)
                
                await self.page.click('button.agree-button')
                
                logger.info("正在等待页面跳转...")
                await self.page.wait_for_url(re.compile(r'business\.gemini\.google/home/cid/'), timeout=90000)
                await asyncio.sleep(3)
                
                logger.info("正在提取凭证数据...")
                cookies = await context.cookies()
                for cookie in cookies:
                    if cookie['name'] == '__Host-C_OSES':
                        self.credential.c_oses = cookie['value']
                    elif cookie['name'] == '__Secure-C_SES':
                        self.credential.c_ses = cookie['value']
                
                current_url = self.page.url
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(current_url)
                self.credential.csesidx = parse_qs(parsed.query).get('csesidx', [''])[0]
                
                path_match = re.search(r'/cid/([a-f0-9-]+)', parsed.path)
                if path_match:
                    self.credential.config_id = path_match.group(1)
                
                if self.credential.is_complete():
                    logger.info(f"✅ 注册成功! 邮箱: {email}")
                    return True
                else:
                    raise Exception("未能获取完整凭证数据")
                    
        except Exception as e:
            logger.error(f"❌ 注册失败: {e}")
            return False

class CredentialSyncer:
    def __init__(self, base_url: str, admin_key: str):
        base_url = base_url.rstrip('/')
        if not base_url.startswith(('http://', 'https://')):
            base_url = f"https://{base_url}"
        self.base_url = base_url
        
        self.admin_key = admin_key
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        self.session.headers.update({'Connection': 'keep-alive'})
    
    def _update_proxy(self):
        proxy = os.environ.get('PROXY', '') or PROXY
        if proxy and not self.session.proxies:
            self.session.proxies = {'http': proxy, 'https': proxy}
            logger.info(f"CredentialSyncer 使用代理: {proxy[:30]}...")
    
    def _request(self, method: str, url: str, **kwargs):
        self._update_proxy()
        for attempt in range(3):
            try:
                return getattr(self.session, method)(url, timeout=30, **kwargs)
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep((2 ** attempt) + 1)
        return None
    
    def sync(self, new_accounts: List[Dict]) -> bool:
        try:
            logger.info("步骤 1/4: 登录...")
            res = self._request('post', f"{self.base_url}/login", 
                data={'admin_key': self.admin_key},
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
            if res.status_code != 200:
                logger.error(f"登录失败: {res.status_code}")
                return False
            logger.info("✅ 登录成功")
            
            logger.info("步骤 2/4: 获取现有凭证...")
            res = self._request('get', f"{self.base_url}/admin/accounts-config")
            existing = res.json().get('accounts', []) if res.status_code == 200 else []
            
            logger.info("步骤 3/4: 合并凭证...")
            accounts_dict = {a['id']: a for a in existing if a.get('id')}
            for account in new_accounts:
                if account.get('id'):
                    accounts_dict[account['id']] = account
            merged = list(accounts_dict.values())
            
            logger.info("步骤 4/4: 上传凭证...")
            res = self._request('put', f"{self.base_url}/admin/accounts-config",
                json=merged, headers={'Content-Type': 'application/json'})
            if res.status_code == 200:
                logger.info(f"✅ 上传成功!")
                return True
            else:
                logger.error(f"上传失败: {res.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"同步失败: {e}")
            return False

async def register_worker(worker_id: int, email_config: Dict) -> Optional[Dict]:
    logger.info(f"[Worker-{worker_id}] 开始注册...")
    registrar = GeminiRegistrar(email_config)
    
    if await registrar.register():
        cred = registrar.credential.to_dict()
        logger.info(f"[Worker-{worker_id}] ✅ 成功: {cred['id']}")
        return cred
    else:
        logger.info(f"[Worker-{worker_id}] ❌ 失败")
        return None

async def register_worker_with_sem(sem: asyncio.Semaphore, worker_id: int, email_config: Dict) -> Optional[Dict]:
    async with sem:
        return await register_worker(worker_id, email_config)

async def main():
    global PROXY
    proxy_process = None
    
    try:
        vless_config = os.environ.get('VLESS_CONFIG', '')
        if vless_config:
            try:
                from proxy_helper import setup_proxy
                logger.info("正在启动VLESS代理...")
                proxy_process = setup_proxy()
                if proxy_process:
                    PROXY = os.environ.get('PROXY', '')
                    logger.info(f"VLESS代理已启动: {PROXY}")
            except Exception as e:
                logger.warning(f"VLESS代理启动失败: {e}")
        
        email_config = {
            'worker_domain': os.environ.get('WORKER_DOMAIN', ''),
            'email_domain': os.environ.get('EMAIL_DOMAIN', ''),
            'admin_password': os.environ.get('ADMIN_PASSWORD', '')
        }
        
        sync_url = os.environ.get('SYNC_URL', '')
        sync_key = os.environ.get('SYNC_KEY', '')
        count = int(os.environ.get('REGISTER_COUNT', '1'))
        concurrent = int(os.environ.get('CONCURRENT', '1'))
        
        if not all([email_config['worker_domain'], email_config['email_domain'], email_config['admin_password']]):
            logger.error("❌ 缺少邮箱配置环境变量!")
            sys.exit(1)
        
        if not sync_url or not sync_key:
            logger.error("❌ 缺少同步API配置!")
            sys.exit(1)
        
        print(f"\n{'='*50}")
        print(f"  Gemini Business 注册机 (GitHub Actions)")
        print(f"  计划注册: {count} 个账号")
        print(f"  并发数: {concurrent}")
        print(f"{'='*50}\n")
        
        credentials = []
        
        if concurrent <= 1:
            for i in range(count):
                cred = await register_worker(i + 1, email_config)
                if cred:
                    credentials.append(cred)
                if i < count - 1:
                    await asyncio.sleep(random.randint(3, 6))
        else:
            sem = asyncio.Semaphore(concurrent)
            tasks = [register_worker_with_sem(sem, i + 1, email_config) for i in range(count)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            credentials = [r for r in results if isinstance(r, dict)]
        
        print(f"\n注册完成: 成功 {len(credentials)} 个\n")
        
        if credentials:
            print("开始同步到远程API...\n")
            syncer = CredentialSyncer(sync_url, sync_key)
            if syncer.sync(credentials):
                print("\n✅ 全部完成!")
            else:
                print("\n❌ 同步失败!")
                sys.exit(1)
        else:
            print("没有成功注册的账号，跳过同步")
            
    finally:
        if proxy_process:
            logger.info("执行清理: 终止后台 sing-box 进程...")
            proxy_process.terminate()
            proxy_process.wait(timeout=3)

if __name__ == '__main__':
    asyncio.run(main())
