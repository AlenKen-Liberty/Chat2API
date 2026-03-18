#!/usr/bin/env python3
"""
Fetch real model lists and quota info from Gemini CLI and Codex accounts.
Groups models that share the same quota percentage + reset time.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from collections import defaultdict

from chat2api.account.gemini_account import (
    list_accounts as list_gemini_accounts,
    ensure_fresh_account as ensure_fresh_gemini,
    GeminiAuthError,
)
from chat2api.account.codex_account import (
    list_accounts as list_codex_accounts,
    ensure_fresh_account as ensure_fresh_codex,
    CodexAuthError,
)

# ── Gemini: reuse quota.py logic from code-orchestra ──

RETRIEVE_USER_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
LOAD_CODE_ASSIST_URL = "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_MODELS_URL = "https://api.openai.com/v1/models"


def fetch_gemini_project_id(access_token: str):
    """loadCodeAssist → project_id + subscription_tier"""
    payload = json.dumps({
        "metadata": {"ideType": "IDE_UNSPECIFIED", "pluginType": "GEMINI", "platform": "PLATFORM_UNSPECIFIED"}
    }).encode()
    req = urllib.request.Request(LOAD_CODE_ASSIST_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "GeminiCLI/1.0.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        project_id = data.get("cloudaicompanionProject")
        tier = None
        paid = data.get("paidTier")
        if paid:
            tier = paid.get("name") or paid.get("id")
        if not tier:
            current = data.get("currentTier")
            if current:
                tier = current.get("name") or current.get("id")
        return project_id, tier
    except Exception as e:
        print(f"  ⚠ loadCodeAssist failed: {e}")
        return None, None


def fetch_gemini_models(access_token: str, project_id: str = None):
    """retrieveUserQuota → per-model quota buckets"""
    payload = json.dumps({"project": project_id} if project_id else {}).encode()
    req = urllib.request.Request(RETRIEVE_USER_QUOTA_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "GeminiCLI/1.0.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:500]
        print(f"  ⚠ retrieveUserQuota HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  ⚠ retrieveUserQuota failed: {e}")
        return None


def fetch_codex_usage(access_token: str, account_id: str):
    """wham/usage → codex quota info"""
    req = urllib.request.Request(CODEX_USAGE_URL, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    if account_id:
        req.add_header("ChatGPT-Account-Id", account_id)
    req.add_header("User-Agent", "CodexBar")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:500]
        print(f"  ⚠ wham/usage HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  ⚠ wham/usage failed: {e}")
        return None


def fetch_codex_models(access_token: str):
    """GET /v1/models → available model list for codex"""
    req = urllib.request.Request(CODEX_MODELS_URL, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("User-Agent", "codex-cli/1.0.0")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:500]
        print(f"  ⚠ /v1/models HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  ⚠ /v1/models failed: {e}")
        return None


def format_reset_time(iso_str: str) -> str:
    """Convert ISO-8601 to human-readable relative time"""
    if not iso_str:
        return "N/A"
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = reset_dt - datetime.now(timezone.utc)
        total_secs = max(0, delta.total_seconds())
        hours = int(total_secs // 3600)
        minutes = int((total_secs % 3600) // 60)
        if hours > 24:
            days = hours // 24
            return f"{days}d {hours % 24}h"
        return f"{hours}h {minutes}m"
    except Exception:
        return iso_str[:19]


def format_unix_reset(ts: int) -> str:
    """Convert unix timestamp to relative time"""
    if not ts:
        return "N/A"
    delta = ts - time.time()
    if delta <= 0:
        return "已重置"
    hours = int(delta // 3600)
    minutes = int((delta % 3600) // 60)
    if hours > 24:
        days = hours // 24
        return f"{days}d {hours % 24}h"
    return f"{hours}h {minutes}m"


def main():
    print("=" * 80)
    print("  Chat2API — 模型列表 & 配额扫描")
    print("=" * 80)

    # ════════════════════════════════════════════
    # GEMINI
    # ════════════════════════════════════════════
    print("\n" + "─" * 80)
    print("  GEMINI CLI 账号")
    print("─" * 80)

    gemini_accounts = list_gemini_accounts()
    if not gemini_accounts:
        print("  ❌ 未找到 Gemini 账号")
    else:
        print(f"  找到 {len(gemini_accounts)} 个账号\n")

    all_gemini_models = {}  # model_name → {percentage, reset_time, accounts}

    for acc in gemini_accounts:
        print(f"  📧 {acc.email} (disabled={acc.disabled})")

        if acc.disabled:
            print(f"    ⚠ 已禁用: {acc.disabled_reason}")
            continue

        # Refresh token
        try:
            acc = ensure_fresh_gemini(acc)
            print(f"    ✅ Token 已刷新")
        except GeminiAuthError as e:
            print(f"    ❌ Token 刷新失败: {e}")
            continue

        # Get project_id
        project_id = acc.project_id
        tier = acc.subscription_tier
        if not project_id:
            project_id, tier = fetch_gemini_project_id(acc.token.access_token)
            if project_id:
                acc.project_id = project_id
                acc.subscription_tier = tier
                print(f"    📋 Project: {project_id}, Tier: {tier}")

        # Fetch models
        raw = fetch_gemini_models(acc.token.access_token, project_id)
        if not raw:
            continue

        buckets = raw.get("buckets", [])
        print(f"    📊 获取到 {len(buckets)} 个模型配额桶\n")

        # Group by (percentage, reset_time) to find shared quotas
        quota_groups = defaultdict(list)
        for bucket in buckets:
            name = bucket.get("modelId", "")
            remaining = bucket.get("remainingFraction", 0.0)
            pct = int(remaining * 100)
            reset = bucket.get("resetTime", "")
            quota_groups[(pct, reset)].append(name)

            if name not in all_gemini_models:
                all_gemini_models[name] = {
                    "percentage": pct,
                    "reset_time": reset,
                    "accounts": [],
                }
            all_gemini_models[name]["accounts"].append(acc.email)

        # Print grouped
        print(f"    {'模型名称':<50} {'配额%':>6}  {'重置时间':>12}")
        print(f"    {'─' * 50} {'─' * 6}  {'─' * 12}")
        for bucket in sorted(buckets, key=lambda b: b.get("modelId", "")):
            name = bucket.get("modelId", "")
            remaining = bucket.get("remainingFraction", 0.0)
            pct = int(remaining * 100)
            reset = bucket.get("resetTime", "")
            print(f"    {name:<50} {pct:>5}%  {format_reset_time(reset):>12}")

        # Show shared quota groups
        print(f"\n    🔗 共享配额组（同百分比 + 同重置时间 = 同一配额池）:")
        for (pct, reset), models in sorted(quota_groups.items(), key=lambda x: -x[0][0]):
            if len(models) > 1:
                print(f"    ┌ [{pct}% | 重置: {format_reset_time(reset)}]")
                for m in sorted(models):
                    print(f"    │  {m}")
                print(f"    └ (这 {len(models)} 个模型共享配额，降级无意义)")
        print()

    # ════════════════════════════════════════════
    # CODEX
    # ════════════════════════════════════════════
    print("\n" + "─" * 80)
    print("  CODEX (OpenAI) 账号")
    print("─" * 80)

    codex_accounts = list_codex_accounts()
    if not codex_accounts:
        print("  ❌ 未找到 Codex 账号")
    else:
        print(f"  找到 {len(codex_accounts)} 个账号\n")

    all_codex_info = []

    for acc in codex_accounts:
        print(f"  📧 {acc.email} (plan={acc.plan_type}, disabled={acc.disabled})")

        if acc.disabled:
            print(f"    ⚠ 已禁用")
            continue

        # Refresh token
        try:
            acc = ensure_fresh_codex(acc)
            print(f"    ✅ Token 已刷新")
        except CodexAuthError as e:
            print(f"    ❌ Token 刷新失败: {e}")
            continue

        # Fetch quota
        usage = fetch_codex_usage(acc.access_token, acc.account_id)
        if usage:
            rate_limit = usage.get("rate_limit") or {}
            primary = rate_limit.get("primary_window") or {}
            secondary = rate_limit.get("secondary_window") or {}
            code_review = (usage.get("code_review_rate_limit") or {}).get("primary_window") or {}

            weekly_used = primary.get("used_percent", "?")
            weekly_reset = primary.get("reset_at", 0)
            burst_used = secondary.get("used_percent", "?")
            burst_reset = secondary.get("reset_at", 0)
            cr_used = code_review.get("used_percent", "?")
            cr_reset = code_review.get("reset_at", 0)

            print(f"    📊 Weekly: {weekly_used}% used (重置: {format_unix_reset(weekly_reset)})")
            print(f"    📊 Burst:  {burst_used}% used (重置: {format_unix_reset(burst_reset)})")
            print(f"    📊 Code Review: {cr_used}% used (重置: {format_unix_reset(cr_reset)})")
            print(f"    📋 Plan: {usage.get('plan_type', '?')}")

            all_codex_info.append({
                "email": acc.email,
                "plan_type": usage.get("plan_type"),
                "weekly_used": weekly_used,
                "weekly_reset": weekly_reset,
                "burst_used": burst_used,
                "burst_reset": burst_reset,
            })

        # Fetch available models
        models_data = fetch_codex_models(acc.access_token)
        if models_data and "data" in models_data:
            model_list = [m.get("id", "") for m in models_data["data"]]
            model_list.sort()
            print(f"\n    📋 可用模型 ({len(model_list)} 个):")
            for m in model_list:
                print(f"       {m}")
        print()

    # ════════════════════════════════════════════
    # SUMMARY
    # ════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("  总结 — 用于 config.yaml 分档决策")
    print("=" * 80)

    print("\n  [Gemini 模型，按配额组分类]")
    if all_gemini_models:
        # Group by (percentage, reset_time)
        groups = defaultdict(list)
        for name, info in all_gemini_models.items():
            groups[(info["percentage"], info["reset_time"])].append(name)

        for (pct, reset), models in sorted(groups.items(), key=lambda x: -x[0][0]):
            reset_str = format_reset_time(reset)
            print(f"\n  配额组 [{pct}% 剩余 | 重置: {reset_str}]:")
            for m in sorted(models):
                print(f"    - {m}")

    print("\n  [Codex 配额概览]")
    for info in all_codex_info:
        print(f"  {info['email']}: weekly {info['weekly_used']}% used, "
              f"burst {info['burst_used']}% used, plan={info['plan_type']}")

    # Dump raw data for reference
    raw_output = {
        "gemini_models": {k: {"percentage": v["percentage"], "reset_time": v["reset_time"]}
                         for k, v in all_gemini_models.items()},
        "codex_accounts": all_codex_info,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    out_path = os.path.join(os.path.dirname(__file__), "..", "model_scan.json")
    with open(out_path, "w") as f:
        json.dump(raw_output, f, indent=2, ensure_ascii=False)
    print(f"\n  📄 原始数据已保存到 model_scan.json")


if __name__ == "__main__":
    main()
