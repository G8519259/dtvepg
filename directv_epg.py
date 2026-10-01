#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import gzip
import json
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Set, Dict
from xml.dom import minidom
import requests

# ==================== 全局配置 ====================

TOKEN_FILE = "directv_token.txt"
OUTPUT_XML = "directv_epg.xml"
OUTPUT_GZ = "directv_epg.xml.gz"
DEFAULT_FILTER_FILE = "DirectTVchannels.txt"

# ==================== 上传配置 ====================
# 上传目标为 pb.nanhui.eu.org 这类 paste 服务，使用 HTTP Basic Auth + multipart 表单字段 c。
#   参考命令:
#   curl -u USER:PASS -X PUT -Fc=@directv_epg.xml.gz URL
#
# 凭据不硬编码进仓库：
#   1) 优先读取环境变量 EPG_UPLOAD_URL / EPG_UPLOAD_USER / EPG_UPLOAD_PASS
#   2) 否则读取配置文件 config.ini 的 [upload] 段（见 config.example.ini）
# config.ini 已被 .gitignore 忽略，不会提交到仓库。
import configparser

CONFIG_FILE = "config.ini"


def _load_upload_config() -> Tuple[str, str, str]:
    """读取上传配置：环境变量优先，其次 config.ini，最后为空字符串。"""
    url = os.environ.get("EPG_UPLOAD_URL", "")
    user = os.environ.get("EPG_UPLOAD_USER", "")
    pwd = os.environ.get("EPG_UPLOAD_PASS", "")

    if (not url or not user or not pwd) and os.path.exists(CONFIG_FILE):
        try:
            cp = configparser.ConfigParser()
            cp.read(CONFIG_FILE, encoding="utf-8")
            if cp.has_section("upload"):
                url = url or cp.get("upload", "url", fallback="")
                user = user or cp.get("upload", "username", fallback="")
                pwd = pwd or cp.get("upload", "password", fallback="")
        except Exception as e:
            print(f"⚠️ 读取配置文件 {CONFIG_FILE} 失败: {e}")

    return url, user, pwd


UPLOAD_URL, UPLOAD_USER, UPLOAD_PASS = _load_upload_config()


def _load_telegram_config() -> Tuple[str, str]:
    """读取 Telegram 通知配置：环境变量优先，其次 config.ini 的 [telegram] 段。

    未配置（缺少 bot_token 或 chat_id）时返回空字符串，脚本将跳过通知。
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if (not token or not chat_id) and os.path.exists(CONFIG_FILE):
        try:
            cp = configparser.ConfigParser()
            cp.read(CONFIG_FILE, encoding="utf-8")
            if cp.has_section("telegram"):
                token = token or cp.get("telegram", "bot_token", fallback="")
                chat_id = chat_id or cp.get("telegram", "chat_id", fallback="")
        except Exception as e:
            print(f"⚠️ 读取配置文件 {CONFIG_FILE} 的 telegram 段失败: {e}")

    return token, chat_id


TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = _load_telegram_config()

# -z all 时使用的内置 ZIP Code 列表（每个代表一个主要区域/RSN）
BUILTIN_ZIP_CODES = [
    "95101",  # 湾区 / 加州 (San Jose / San Francisco)
    "10001",  # 纽约大都会 (New York)
    "90001",  # 洛杉矶 (Los Angeles)
    "60601",  # 芝加哥 (Chicago)
    "02108",  # 波士顿 / 新英格兰 (Boston)
]

# 自动获取 Guest Token 接口
GUEST_TOKEN_URL = "https://api.cld.dtvce.com/authn-tokengo/v3/v2/tokens?client_id=DTVE_DFW_WEB_Chrome_G"
DIRECTV_REFRESH_URL = "https://www.directv.com/api/user/v1/session/refresh"

BASE_CHANNELS_URL = "https://api.cld.dtvce.com/discovery/metadata/channel/v5/service/allchannels"
SCHEDULE_URL = "https://api.cld.dtvce.com/discovery/edge/schedule/v1/service/schedule"

BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "origin": "https://www.directv.com",
    "priority": "u=1, i",
    "referer": "https://www.directv.com/",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
}

# ==================== Context 动态构建 ====================

def build_client_context(zip_code: str, dma_id: str = "803_0", billing_dma: str = "803", state: str = "CA", county_code: str = "037") -> str:
    """根据输入的 ZIP Code 动态生成 DirecTV 的 clientContext"""
    # 如果输入的 dma_id 不带后缀，补全 _0
    formatted_dma = dma_id if "_" in dma_id else f"{dma_id}_0"
    clean_billing_dma = billing_dma.split("_")[0]
    
    return f"dmaID:{formatted_dma},billingDmaID:{clean_billing_dma},zipCode:{zip_code},countyCode:{county_code},stateNumber:6,stateAbbr:{state},usrLocAndBillLocAreSame:true,deviceProximity:OOH"

# ==================== Token 管理与自动刷新 ====================

def load_tokens() -> Tuple[Optional[str], Optional[str]]:
    if not os.path.exists(TOKEN_FILE):
        return None, None

    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return None, None

            if content.startswith("{"):
                data = json.loads(content)
                return data.get("access_token"), data.get("refresh_token")
            return content, None
    except Exception as e:
        print(f"⚠️ 读取 {TOKEN_FILE} 异常: {e}")
        return None, None


def save_tokens(access_token: str, refresh_token: Optional[str] = None):
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            if refresh_token:
                json.dump(
                    {"access_token": access_token, "refresh_token": refresh_token},
                    f,
                    indent=2,
                )
            else:
                f.write(access_token)
        print(f"💾 Token 已成功更新保存至 {TOKEN_FILE}")
    except Exception as e:
        print(f"⚠️ 保存 Token 失败: {e}")


def fetch_new_guest_token() -> Optional[Tuple[str, str]]:
    headers = BASE_HEADERS.copy()
    headers["content-type"] = "application/json"

    try:
        print("🔄 正在自动向 DirecTV 申请全新的 Guest Token...")
        resp = requests.post(GUEST_TOKEN_URL, json={}, headers=headers, timeout=12)
        if resp.status_code in (200, 201):
            data = resp.json()
            new_access = data.get("access_token")
            new_refresh = data.get("refresh_token")

            if new_access:
                print("✅ 成功自动获取全新 Guest Token！")
                save_tokens(new_access, new_refresh)
                return new_access, new_refresh
        else:
            print(f"⚠️ 自动获取 Guest Token 失败, HTTP 状态码: {resp.status_code}")
    except Exception as e:
        print(f"⚠️ 请求 Guest Token 发生异常: {e}")

    return None


def try_refresh_token(refresh_token: str) -> Optional[str]:
    res = fetch_new_guest_token()
    if res and res[0]:
        return res[0]

    if refresh_token:
        headers = BASE_HEADERS.copy()
        headers["content-type"] = "application/json"
        payload = {"refresh_token": refresh_token}

        try:
            resp = requests.post(
                DIRECTV_REFRESH_URL, json=payload, headers=headers, timeout=12
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                new_access = data.get("access_token")
                new_refresh = data.get("refresh_token", refresh_token)

                if new_access:
                    print("✅ 成功自动续期 access_token！")
                    save_tokens(new_access, new_refresh)
                    return new_access
        except Exception:
            pass

    return None


def get_headers() -> dict:
    access_token, refresh_token = load_tokens()

    if not access_token:
        print("ℹ️ 本地未发现有效 Token，开始自动获取...")
        res = fetch_new_guest_token()
        if res:
            access_token = res[0]
    else:
        new_token = try_refresh_token(refresh_token)
        if new_token:
            access_token = new_token

    if not access_token:
        raise RuntimeError("❌ 无法获取有效的 Access Token，请检查网络或接口是否变更！")

    headers = BASE_HEADERS.copy()
    headers["authorization"] = f"Bearer {access_token}"
    return headers

# ==================== 工具函数 ====================

import re

# 括号内容匹配（捕获内部文本）：(…) 或 […]
_PARENS_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
# 单独的画质标签（整段等于 HD/SD）。
# 注意：4K / UHD 不在此列——它们通常是独立的频道/独立节目源，需保留以区分。
_QUALITY_ONLY_RE = re.compile(r"^(hd|sd)$", re.IGNORECASE)
# 作为独立单词出现的画质标签（用于从任意位置移除）。同样不包含 4K / UHD。
_QUALITY_WORD_RE = re.compile(r"\b(hd|sd)\b", re.IGNORECASE)


def _is_noise_paren(content: str) -> bool:
    """判断括号内容是否为“噪音”（应删除）：
      - 空
      - 画质标签 HD/SD（但不含 4K/UHD——它们视为不同频道，予以保留）
      - 台号/代码：含数字且仅由字母数字/短横线/空格组成，且不含长度≥3 的字母单词
        例如 '103A' '99R' '98-4' '213-2' -> 删除
    保留有意义的词，如 'Alternate' 'East' 'West' 'Los Angeles' 'ABC' 'Steve Harvey'。
    """
    c = content.strip()
    if not c:
        return True
    if _QUALITY_ONLY_RE.match(c):
        return True
    if re.search(r"\d", c) and re.fullmatch(r"[0-9A-Za-z\- ]+", c):
        # 含数字的代码型内容；若其中包含长度≥3 的字母单词则视为有意义（如 'Alternate 2'）予以保留
        if re.search(r"[A-Za-z]{3,}", c):
            return False
        return True
    return False


def clean_display_name(name: str) -> str:
    """清洗用于 XMLTV <display-name> 的频道名（与去重规则一致）：
      - 删除“噪音”括号：画质标签 (HD)/(SD) 与台号代码 (103A)/(99R)/(98-4) 等
      - 保留有意义括号：(Alternate) / (East) / (West) / (Los Angeles) / (ABC) 等
      - 删除任意位置作为独立单词出现的画质标签 HD/SD（但保留 4K / UHD 以区分频道）
      - 压缩多余空白
    例如:
      'Cinemax Classics HD'            -> 'Cinemax Classics'
      'CNN en Espanol (103A)'          -> 'CNN en Espanol'
      'Altitude Sports HD (Alternate)' -> 'Altitude Sports (Alternate)'   （保留 Alternate）
      'FOX 4K' / 'FOX UHD'             -> 'FOX 4K' / 'FOX UHD'             （保留，不与 FOX 合并）
    若清洗后为空，则回退为原始名称，避免出现空的 display-name。
    """
    original = (name or "").strip()

    def _repl(m):
        return "" if _is_noise_paren(m.group(1)) else m.group(0)

    n = _PARENS_RE.sub(_repl, original)      # 删除噪音括号，保留有意义括号
    n = _QUALITY_WORD_RE.sub("", n)          # 删除任意位置的 HD/SD/4K/UHD 独立单词
    n = re.sub(r"\s+", " ", n).strip()
    return n if n else original


def normalize_channel_name(name: str) -> str:
    """归一化频道名用于去重：采用与 clean_display_name 完全一致的清洗规则后转小写。

    这样去重键与 <display-name> 输出保持同一套规则：
      - 去掉画质标签 HD/SD 与噪音台号括号 (103A)/(99R)/(98-4)
      - 保留 4K/UHD 以及有意义括号 (Alternate)/(East)/(Los Angeles)/(ABC) 等
    因此:
      'CNN' / 'CNN HD'                 -> 'cnn'                     （合并）
      'FOX 4K' / 'FOX UHD'             -> 'fox 4k' / 'fox uhd'      （与 FOX 区分，不合并）
      'CNN en Espanol (103A)'          -> 'cnn en espanol'          （与 CNN 区分）
      'Altitude Sports HD'             -> 'altitude sports'
      'Altitude Sports HD (Alternate)' -> 'altitude sports (alternate)'  （与主频道区分，不合并）
    """
    return clean_display_name(name).lower()


def is_hd_channel(ch_name: str, call_sign: str = "") -> bool:
    """判断该频道是否为 HD 版本（用于同一频道 SD/HD 二选一时优先保留 HD）。

    仅依据 HD 标记；4K/UHD 已被视为不同频道（归一化后 key 不同），
    不会与基础频道参与同一次 SD/HD 取舍，故此处无需匹配 4K/UHD。
    """
    text = f"{ch_name or ''} {call_sign or ''}".upper()
    return bool(re.search(r"\bHD\b", text))


def load_channel_filter(file_path: str) -> Set[str]:
    if not os.path.exists(file_path):
        print(f"⚠️ 找不到白名单文件 {file_path}，将放弃过滤抓取全量频道。")
        return set()

    filter_names = set()
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name and not name.startswith("#"):
                    filter_names.add(name)
        print(f"📄 已成功载入 {len(filter_names)} 个过滤频道名单 ({file_path})。")
    except Exception as e:
        print(f"⚠️ 读取 {file_path} 失败: {e}")

    return filter_names


def parse_to_xmltv_time(time_val):
    if not time_val:
        return ""
    try:
        if isinstance(time_val, str) and ("T" in time_val or "-" in time_val):
            clean_str = time_val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_str).astimezone(timezone.utc)
            dt = dt - timedelta(hours=1)
            return dt.strftime("%Y%m%d%H%M%S +0000")

        ts = float(time_val)
        if ts > 1e11:
            ts /= 1000.0
        ts -= 3600
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y%m%d%H%M%S +0000")
    except Exception:
        return ""


def format_date_str(date_val):
    if not date_val:
        return ""
    try:
        date_str = str(date_val).strip()
        if "-" in date_str:
            parts = date_str.split("T")[0].split("-")
            if len(parts) >= 3:
                return f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
        elif len(date_str) >= 8 and date_str[:8].isdigit():
            return date_str[:8]
        elif date_str.isdigit() and len(date_str) > 10:
            ts = float(date_str) / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    except Exception:
        pass
    return ""


def extract_channel_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in [
            "channelInfoList",
            "channels",
            "channelList",
            "chList",
            "data",
            "items",
        ]:
            if key in data and isinstance(data[key], list):
                return data[key]
        for val in data.values():
            if isinstance(val, dict):
                res = extract_channel_list(val)
                if res:
                    return res
            elif isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                return val
    return []


def get_channel_info(ch):
    ch_num = str(ch.get("channelNumber") or ch.get("chNumber") or "").strip()
    real_channel_id = str(ch.get("resourceId") or ch.get("channelId") or "").strip()
    ccid = str(ch.get("ccid") or "").strip()
    ch_name = (
        ch.get("channelName") or ch.get("networkName") or f"Channel {ch_num}"
    )
    call_sign = str(ch.get("callSign") or "").strip()

    logo_url = ""
    image_list = ch.get("imageList")
    if isinstance(image_list, list) and len(image_list) > 0:
        type_priority = [
            "chlogo-bwdb-player",
            "chlogo-bwdb-fplayer",
            "chlogo-cdb-gcd",
            "chlogo-clb-guide",
            "chlogo-bwdb-mytv",
        ]

        best_img = None
        best_score = -1

        for img in image_list:
            if isinstance(img, dict):
                img_url = img.get("imageUrl") or img.get("defaultImageUrl")
                img_type = str(img.get("imageType") or "").lower()

                if img_url:
                    score = 0
                    for idx, key in enumerate(type_priority):
                        if key in img_type:
                            score = len(type_priority) - idx
                            break
                    if score > best_score:
                        best_score = score
                        best_img = img_url

        logo_url = (
            best_img
            if best_img
            else (
                image_list[0].get("imageUrl")
                if isinstance(image_list[0], dict)
                else ""
            )
        )

    if logo_url:
        logo_url = str(logo_url).strip()
        if logo_url.startswith("//"):
            logo_url = "https:" + logo_url
        elif logo_url.startswith("/"):
            logo_url = "https://dfwfis.prod.dtvcdn.com" + logo_url
        elif not logo_url.startswith("http"):
            logo_url = "https://" + logo_url

    return ch_num, real_channel_id, ccid, ch_name, call_sign, logo_url

# ==================== 网络请求 ====================

def fetch_channels(headers: dict, client_context: str):
    """拉取指定区域的频道列表"""
    params = {
        "sort": "OrdCh=ASC",
        "clientContext": client_context,
        "fisProperties": "ISF:1.0#chlogo-bwdb-mytv,47,35#chlogo-clb-guide,60,45#chlogo-cdb-gcd,87,66#chlogo-bwdb-player,120,91#chlogo-bwdb-fplayer,120,91",
        "include4K": "false",
        "is4KCompatible": "false"
    }

    print(f"正在拉取 DirecTV 频道列表 (Context: {client_context[:45]}...)...")
    try:
        resp = requests.get(BASE_CHANNELS_URL, headers=headers, params=params, timeout=15)

        if resp.status_code == 401:
            print("⚠️ 遇到 401 Unauthorized，Token 可能已失效，正在重新申请...")
            token_res = fetch_new_guest_token()
            if token_res and token_res[0]:
                headers["authorization"] = f"Bearer {token_res[0]}"
                resp = requests.get(BASE_CHANNELS_URL, headers=headers, params=params, timeout=15)

        if resp.status_code != 200:
            print(f"❌ [ERROR] 频道接口响应失败，HTTP 状态码: {resp.status_code}")
            return []

        raw_data = resp.json()
        channels = extract_channel_list(raw_data)
        print(f"✅ 成功解析到 {len(channels)} 个频道。")
        return channels
    except Exception as e:
        print(f"❌ [ERROR] 拉取频道列表异常: {e}")
        return []


def fetch_schedule(channel_ids, start_ms, end_ms, headers: dict, client_context: str):
    """拉取指定区域的节目单数据"""
    params = {
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "clientContext": client_context,
        "fisProperties": "ISF:2.0#bg-fplayer,1024,576#iconic,32,18",
        "channelIds": ",".join(channel_ids),
        "include4K": "false",
        "is4Kcompatible": "false",
        "includeTVOD": "true",
        "_tz": str(int(time.time() * 1000)),
    }

    try:
        resp = requests.get(SCHEDULE_URL, headers=headers, params=params, timeout=15)

        if resp.status_code == 401:
            print("\n⚠️ [401 过期防御] 抓取中途 Token 过期，正在自动刷新 Token 并重试...")
            token_res = fetch_new_guest_token()
            if token_res and token_res[0]:
                headers["authorization"] = f"Bearer {token_res[0]}"
                resp = requests.get(SCHEDULE_URL, headers=headers, params=params, timeout=15)

        if resp.status_code != 200:
            print(f"\n⚠️ [ERROR] Schedule HTTP Status: {resp.status_code} | Body: {resp.text[:200]}")
            return None, resp.text

        data = resp.json()
        if isinstance(data, dict):
            return data.get("schedules") or [], data
        elif isinstance(data, list):
            return data, data
        return [], data
    except Exception as e:
        print(f"\n❌ [ERROR] 请求 Schedule 发生异常: {e}")
        return None, str(e)


def parse_program(prog):
    if not isinstance(prog, dict):
        return None

    title = (
        prog.get("title")
        or prog.get("titleName")
        or prog.get("seriesTitle")
        or prog.get("programTitle")
        or prog.get("name")
        or (
            (prog.get("program") or {}).get("title")
            if isinstance(prog.get("program"), dict)
            else None
        )
    )

    subtitle = (
        prog.get("episodeTitle") or prog.get("secondaryTitle") or prog.get("subtitle")
    )

    if not title and subtitle:
        title = subtitle
        subtitle = None
    elif not title:
        title = "Unknown Program"

    start_time_raw = None
    end_time_raw = None

    consumables = prog.get("consumables")
    if isinstance(consumables, list) and len(consumables) > 0:
        c0 = consumables[0]
        if isinstance(c0, dict):
            start_time_raw = c0.get("startTime")
            end_time_raw = c0.get("endTime")

    if not start_time_raw:
        start_time_raw = (
            prog.get("startTime") or prog.get("airTime") or prog.get("start")
        )
    if not end_time_raw:
        end_time_raw = prog.get("endTime") or prog.get("stop")

    start_xml = parse_to_xmltv_time(start_time_raw)
    end_xml = parse_to_xmltv_time(end_time_raw)

    desc = prog.get("description") or prog.get("shortDescription") or prog.get("desc")

    categories = []
    raw_cats = prog.get("categories")
    if isinstance(raw_cats, list):
        categories = [str(c) for c in raw_cats if c]
    elif isinstance(prog.get("category"), str):
        categories = [prog.get("category")]

    raw_date = (
        prog.get("releaseDate")
        or prog.get("airDate")
        or prog.get("originalAirDate")
        or prog.get("releaseYear")
    )
    date_formatted = format_date_str(raw_date)

    season_num = prog.get("seasonNumber") or prog.get("season")
    episode_num = prog.get("episodeNumber") or prog.get("episode")

    if (not season_num or not episode_num) and isinstance(prog.get("program"), dict):
        p_obj = prog.get("program")
        season_num = season_num or p_obj.get("seasonNumber") or p_obj.get("season")
        episode_num = episode_num or p_obj.get("episodeNumber") or p_obj.get("episode")

    return {
        "title": title,
        "start": start_xml,
        "stop": end_xml,
        "subtitle": subtitle,
        "desc": desc,
        "categories": categories,
        "date": date_formatted,
        "season": int(season_num) if str(season_num).isdigit() else None,
        "episode": int(episode_num) if str(episode_num).isdigit() else None,
    }

# ==================== 压缩与上传 ====================

def gzip_file(src_path: str, dst_path: str) -> bool:
    """将 src_path gzip 压缩为 dst_path。成功返回 True。"""
    try:
        print(f"正在压缩生成 {dst_path} ...")
        with open(src_path, "rb") as f_in:
            with gzip.open(dst_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        print(f"🎉 成功生成压缩包文件：{dst_path}")
        return True
    except Exception as e:
        print(f"⚠️ 压缩生成 {dst_path} 失败: {e}")
        return False


def upload_file(file_path: str) -> bool:
    """通过 HTTP PUT + multipart 表单字段 c 上传文件到 paste 服务（HTTP Basic Auth）。

    等价于：
      curl -u USER:PASS -X PUT -Fc=@file URL
    """
    if not os.path.exists(file_path):
        print(f"⚠️ 待上传文件不存在: {file_path}")
        return False

    if not UPLOAD_URL or not UPLOAD_USER or not UPLOAD_PASS:
        print(
            "⚠️ 未配置上传信息，已跳过上传。\n"
            "   请设置环境变量 EPG_UPLOAD_URL/EPG_UPLOAD_USER/EPG_UPLOAD_PASS，\n"
            "   或复制 config.example.ini 为 config.ini 并填入真实值。"
        )
        return False

    print(f"☁️ 正在上传 {file_path} 到 {UPLOAD_URL} ...")
    try:
        with open(file_path, "rb") as f:
            files = {"c": (os.path.basename(file_path), f, "application/gzip")}
            resp = requests.put(
                UPLOAD_URL,
                auth=(UPLOAD_USER, UPLOAD_PASS),
                files=files,
                timeout=60,
            )
        if 200 <= resp.status_code < 300:
            print(f"✅ 上传成功 (HTTP {resp.status_code})。响应: {resp.text[:200]}")
            return True
        print(f"❌ 上传失败，HTTP {resp.status_code}。响应: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"❌ 上传发生异常: {e}")
        return False


# ==================== Telegram 通知 ====================

def send_telegram_message(text: str) -> bool:
    """通过 Telegram Bot API 发送一条消息。

    仅当配置了 bot_token 与 chat_id 时才发送；否则静默跳过。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # 未配置 Telegram，跳过（这是可选功能）
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    print("📨 正在发送 Telegram 通知...")
    try:
        resp = requests.post(api_url, json=payload, timeout=20)
        if resp.status_code == 200 and resp.json().get("ok"):
            print("✅ Telegram 通知发送成功。")
            return True
        print(f"❌ Telegram 通知发送失败，HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"❌ 发送 Telegram 通知发生异常: {e}")
        return False


def human_size(num_bytes: int) -> str:
    """将字节数格式化为人类可读大小。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser(description="DirecTV EPG 抓取生成工具")
    parser.add_argument(
        "-z",
        "--zip",
        type=str,
        default="95101",
        help="指定抓取的 ZIP Code 邮编 (默认为 95101)；传入 'all' 则遍历内置多区域 ZIP 列表并去重合并",
    )
    parser.add_argument(
        "-d", "--days", type=int, default=3, help="抓取 EPG 的天数 (默认为 3 天)"
    )
    parser.add_argument(
        "-f",
        "--filter",
        nargs="?",
        const=DEFAULT_FILTER_FILE,
        default=None,
        help="指定仅抓取的频道文件列表（例如: DirectTVchannels.txt）",
    )
    parser.add_argument(
        "-u",
        "--upload",
        action="store_true",
        help="生成 .gz 后自动上传到 paste 服务 (使用 EPG_UPLOAD_URL/USER/PASS 环境变量或内置默认值)",
    )
    args = parser.parse_args()

    # 确定要抓取的 ZIP Code 列表：'all' -> 内置多区域列表；否则单个 ZIP
    if args.zip.strip().lower() == "all":
        zip_list = list(BUILTIN_ZIP_CODES)
        print(f"🌐 目标区域 ZIP Code: ALL -> {zip_list}")
    else:
        zip_list = [args.zip.strip()]
        print(f"🌐 目标区域 ZIP Code: {args.zip}")

    filter_names = set()
    if args.filter:
        filter_names = load_channel_filter(args.filter)

    try:
        headers = get_headers()
    except Exception as e:
        print(f"❌ 初始化 Headers 失败: {e}")
        return

    # 生成时间（UTC）。
    #   - date: 采用完整 XMLTV 时间格式 YYYYMMDDHHMMSS +0000（XMLTV 规范允许），标识文件生成时间
    #   - generated-at: 额外提供 ISO 8601 时间戳，便于阅读与其他工具解析
    generated_dt = datetime.now(timezone.utc)
    generated_xmltv = generated_dt.strftime("%Y%m%d%H%M%S +0000")
    generated_iso = generated_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "DirecTV-EPG-Generator",
            "generator-info-url": "https://www.directv.com/",
            "date": generated_xmltv,
            "generated-at": generated_iso,
        },
    )

    # 去重后的频道数据结构
    #   selected: dedup_key -> 频道记录 dict（含 xml_id / resource_id / source_zip / is_hd 等）
    #   ccid_index / name_index: 用于快速定位已存在的重复项
    selected: Dict[str, dict] = {}
    ccid_index: Dict[str, str] = {}   # ccid -> dedup_key
    name_index: Dict[str, str] = {}   # normalized_name -> dedup_key

    total_seen = 0

    for zip_code in zip_list:
        ctx = build_client_context(zip_code=zip_code)
        print(f"\n🗺️  === 抓取区域 ZIP {zip_code} 的频道列表 ===")
        channels = fetch_channels(headers, ctx)
        if not channels:
            print(f"⚠️ ZIP {zip_code} 未获取到任何频道，跳过。")
            continue

        for ch in channels:
            if not isinstance(ch, dict):
                continue

            ch_num, real_channel_id, ccid, ch_name, call_sign, logo_url = get_channel_info(ch)
            if not real_channel_id:
                continue

            if filter_names:
                name_clean = ch_name.strip()
                call_clean = call_sign.strip()
                if name_clean not in filter_names and call_clean not in filter_names:
                    continue

            total_seen += 1

            norm_name = normalize_channel_name(ch_name)
            hd = is_hd_channel(ch_name, call_sign)

            # 判断是否与已选频道重复：优先 ccid，其次归一化名称
            dup_key = None
            if ccid and ccid in ccid_index:
                dup_key = ccid_index[ccid]
            elif norm_name and norm_name in name_index:
                dup_key = name_index[norm_name]

            record = {
                "xml_id": ccid if ccid else (ch_num if ch_num else real_channel_id),
                "resource_id": real_channel_id,
                "ccid": ccid,
                "ch_num": ch_num,
                "ch_name": ch_name.strip(),
                "call_sign": call_sign,
                "logo_url": logo_url,
                "source_zip": zip_code,
                "is_hd": hd,
                "norm_name": norm_name,
            }

            if dup_key is None:
                # 全新频道
                key = ccid if ccid else f"name::{norm_name}"
                selected[key] = record
                if ccid:
                    ccid_index[ccid] = key
                if norm_name:
                    name_index[norm_name] = key
            else:
                # 与已选频道重复：仅当“新的是 HD 而旧的是 SD”时才替换（HD 优先）
                existing = selected[dup_key]
                if hd and not existing["is_hd"]:
                    # 用 HD 版本替换旧的 SD 记录，并更新索引
                    old_ccid = existing.get("ccid")
                    old_norm = existing.get("norm_name")
                    selected[dup_key] = record
                    if old_ccid and ccid_index.get(old_ccid) == dup_key and old_ccid != ccid:
                        ccid_index.pop(old_ccid, None)
                    if ccid:
                        ccid_index[ccid] = dup_key
                    if old_norm and name_index.get(old_norm) == dup_key and old_norm != norm_name:
                        name_index.pop(old_norm, None)
                    if norm_name:
                        name_index[norm_name] = dup_key
                # 否则保留已有版本（旧的是 HD，或两者同级）

    if not selected:
        print("❌ 所有区域均未匹配到有效频道，程序退出。")
        return

    # 构建 XMLTV <channel> 元素，并生成节目抓取所需的列表
    #   cid_to_num: resourceId -> xml_id（节目单以 resourceId 为键）
    #   channel_info_list: [(xml_id, resource_id, source_zip), ...]
    cid_to_num = {}
    channel_info_list = []
    icon_count = 0

    for rec in selected.values():
        xml_id = rec["xml_id"]
        cid_to_num[rec["resource_id"]] = xml_id
        channel_info_list.append((xml_id, rec["resource_id"], rec["source_zip"]))

        ch_elem = ET.SubElement(tv, "channel", id=xml_id)

        # 频道名按去重规则清洗（去括号内容与尾部 HD/SD/4K/UHD 标签）
        display_name = clean_display_name(rec["ch_name"])
        name_elem = ET.SubElement(ch_elem, "display-name")
        name_elem.text = display_name

        if rec["ch_num"]:
            num_elem = ET.SubElement(ch_elem, "display-name")
            num_elem.text = rec["ch_num"]

        if rec["call_sign"] and rec["call_sign"] != display_name and rec["call_sign"] != rec["ch_num"]:
            cs_elem = ET.SubElement(ch_elem, "display-name")
            cs_elem.text = rec["call_sign"]

        if rec["logo_url"]:
            ET.SubElement(ch_elem, "icon", src=rec["logo_url"])
            icon_count += 1

    if len(zip_list) > 1:
        print(
            f"\n🧹 多区域去重完成！共扫描 {total_seen} 个频道条目，去重后保留 {len(channel_info_list)} 个唯一频道（HD 优先）。"
        )
    if filter_names:
        print(f"🎯 已应用过滤列表，最终保留 {len(channel_info_list)} 个符合要求的频道。")

    if not channel_info_list:
        print("❌ 未能匹配到任何有效的频道，请检查频道过滤列表。")
        return

    print(
        f"✅ 频道列表处理完成！在 {len(channel_info_list)} 个频道中，成功抓取到 {icon_count} 个频道的 Icon。"
    )

    days_to_fetch = max(1, args.days)
    ms_per_day = 24 * 60 * 60 * 1000
    base_now_ms = int(time.time() * 1000)

    print(f"准备分批次拉取未来 {days_to_fetch} 天的 EPG 节目数据...")

    chunk_size = 10
    total_program_count = 0

    # 按来源 ZIP 分组：每个频道的节目单必须用其来源区域的 clientContext 抓取
    zip_to_channels: Dict[str, list] = {}
    for xml_id, res_id, src_zip in channel_info_list:
        zip_to_channels.setdefault(src_zip, []).append((xml_id, res_id))

    first_batch_checked = False

    for day_idx in range(days_to_fetch):
        start_ms = base_now_ms + (day_idx * ms_per_day)
        end_ms = start_ms + ms_per_day

        print(f"\n📅 === 开始拉取第 {day_idx + 1}/{days_to_fetch} 天的数据 ===")

        for src_zip, ch_items in zip_to_channels.items():
            zip_ctx = build_client_context(zip_code=src_zip)
            if len(zip_to_channels) > 1:
                print(f"  ↳ 区域 ZIP {src_zip}: {len(ch_items)} 个频道")

            for i in range(0, len(ch_items), chunk_size):
                chunk = ch_items[i : i + chunk_size]
                chunk_cids = [item[1] for item in chunk if item[1]]

                if not chunk_cids:
                    continue

                print(
                    f"进度 [Day {day_idx + 1}][ZIP {src_zip}]: [{min(i + chunk_size, len(ch_items))}/{len(ch_items)}]..."
                )

                schedules, _ = fetch_schedule(chunk_cids, start_ms, end_ms, headers, zip_ctx)
                batch_prog_count = 0

                if schedules:
                    for item in schedules:
                        if not isinstance(item, dict):
                            continue

                        raw_item_id = str(
                            item.get("channelId") or item.get("id") or ""
                        ).strip()
                        xml_ch_id = cid_to_num.get(raw_item_id, raw_item_id)

                        programs = (
                            item.get("contents")
                            or item.get("schedules")
                            or item.get("programs")
                            or item.get("airings")
                            or []
                        )

                        for prog in programs:
                            p_data = parse_program(prog)
                            if not p_data or not p_data["start"] or not p_data["stop"]:
                                continue

                            prog_elem = ET.SubElement(
                                tv,
                                "programme",
                                {
                                    "start": p_data["start"],
                                    "stop": p_data["stop"],
                                    "channel": xml_ch_id,
                                },
                            )

                            title_elem = ET.SubElement(prog_elem, "title", lang="en")
                            title_elem.text = p_data["title"]

                            if p_data["subtitle"] and p_data["subtitle"] != p_data["title"]:
                                sub_elem = ET.SubElement(prog_elem, "sub-title", lang="en")
                                sub_elem.text = p_data["subtitle"]

                            if p_data["desc"]:
                                desc_elem = ET.SubElement(prog_elem, "desc", lang="en")
                                desc_elem.text = p_data["desc"]

                            if p_data["date"]:
                                date_elem = ET.SubElement(prog_elem, "date")
                                date_elem.text = p_data["date"]

                            for cat in p_data["categories"]:
                                cat_elem = ET.SubElement(prog_elem, "category", lang="en")
                                cat_elem.text = cat

                            if p_data["season"] is not None and p_data["episode"] is not None:
                                s = p_data["season"]
                                e = p_data["episode"]

                                xmltv_ns_elem = ET.SubElement(
                                    prog_elem, "episode-num", system="xmltv_ns"
                                )
                                xmltv_ns_elem.text = (
                                    f"{s - 1}.{e - 1}.0/1" if s > 0 and e > 0 else f"{s}.{e}."
                                )

                                onscreen_elem = ET.SubElement(
                                    prog_elem, "episode-num", system="onscreen"
                                )
                                onscreen_elem.text = f"S{s:02d}E{e:02d}"

                            batch_prog_count += 1
                            total_program_count += 1

                # 首个批次做一次健全性检查（解析出的节目数为 0 则中断）
                if not first_batch_checked:
                    first_batch_checked = True
                    if batch_prog_count > 0:
                        print(
                            f"🎉 第一批测试成功！解析出 {batch_prog_count} 条节目记录。继续批量提取...\n"
                        )
                    else:
                        print(
                            "\n❌ [ASSERT FAIL] 第一批数据解析出来的节目数量为 0！强行中断。"
                        )
                        sys.exit(1)

                time.sleep(0.05)

    print(f"\n✅ 节目单全部解析完成，共生成 {total_program_count} 条节目记录。")

    print(f"正在写入 {OUTPUT_XML} ...")
    raw_xml_str = ET.tostring(tv, encoding="utf-8")
    parsed_dom = minidom.parseString(raw_xml_str)
    pretty_xml = parsed_dom.toprettyxml(indent="  ", encoding="utf-8")

    with open(OUTPUT_XML, "wb") as f:
        f.write(pretty_xml)

    print(f"🎉 成功生成 XMLTV 文件：{OUTPUT_XML}")

    # 默认生成 gzip 压缩包（.gz 为默认最终产物）
    gz_ok = gzip_file(OUTPUT_XML, OUTPUT_GZ)

    # 如指定 --upload，则将生成的 .gz 上传到 paste 服务
    uploaded = False
    if args.upload:
        if gz_ok:
            uploaded = upload_file(OUTPUT_GZ)
        else:
            print("⚠️ 由于 .gz 生成失败，已跳过上传。")

    # 如配置了 Telegram bot_token 与 chat_id，则发送本次抓取的汇总信息
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        gz_size = human_size(os.path.getsize(OUTPUT_GZ)) if gz_ok and os.path.exists(OUTPUT_GZ) else "N/A"
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        zips_str = ", ".join(zip_list)
        upload_line = ""
        if args.upload:
            upload_line = f"\n☁️ <b>Upload:</b> {'success' if uploaded else 'failed'}"

        message = (
            "📺 <b>DirecTV EPG updated</b>\n"
            f"🗺️ <b>Regions (ZIPs):</b> {zips_str}\n"
            f"📅 <b>Days:</b> {days_to_fetch}\n"
            f"📡 <b>Channels:</b> {len(channel_info_list)}\n"
            f"🎬 <b>Programmes:</b> {total_program_count}\n"
            f"🖼️ <b>Icons:</b> {icon_count}\n"
            f"🗜️ <b>File:</b> {OUTPUT_GZ} ({gz_size})"
            f"{upload_line}\n"
            f"🕒 <b>Generated:</b> {now_utc}"
        )
        send_telegram_message(message)


if __name__ == "__main__":
    main()
