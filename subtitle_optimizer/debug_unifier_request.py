"""调试：发送术语统一器请求并显示完整响应"""
import json
import time
import requests

# 读取保存的请求
with open("logs/debug_requests/unifier_request.json", encoding="utf-8") as f:
    messages = json.load(f)

# 读取配置
with open("C:/Users/xuan/.subtitle_optimizer_config.json", encoding="utf-8") as f:
    cfg = json.load(f)

url = f"{cfg['base_url']}/chat/completions"
headers = {
    "Authorization": f"Bearer {cfg['api_key']}",
    "Content-Type": "application/json",
}
payload = {
    "model": cfg["model"],
    "messages": messages,
    "temperature": 0.5,
    "reasoning_effort": "xhigh",
}

print(f"URL: {url}")
print(f"Model: {cfg['model']}")
print(f"Messages: {len(messages)} 条")
print(f"System prompt: {len(messages[0]['content'])} chars")
print(f"User prompt: {len(messages[1]['content'])} chars")
print("-" * 60)

start = time.time()
try:
    resp = requests.post(url, headers=headers, json=payload, timeout=360)
    elapsed = time.time() - start
    print(f"Status: {resp.status_code}")
    print(f"耗时: {elapsed:.1f}s")

    if resp.status_code == 200:
        data = resp.json()
        usage = data.get("usage", {})
        print(f"\n=== Token 用量 ===")
        print(f"prompt_tokens: {usage.get('prompt_tokens', '?')}")
        print(f"completion_tokens: {usage.get('completion_tokens', '?')}")
        print(f"total_tokens: {usage.get('total_tokens', '?')}")
        if "completion_tokens_details" in usage:
            details = usage["completion_tokens_details"]
            print(f"reasoning_tokens: {details.get('reasoning_tokens', '?')}")

        content = data["choices"][0]["message"]["content"]
        print(f"\n=== 响应内容 ({len(content)} chars) ===")
        print(content[:3000])
        if len(content) > 3000:
            print(f"\n... (截断，共 {len(content)} chars)")

        # 保存完整响应
        out_path = "logs/debug_requests/unifier_response.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n完整响应已保存到: {out_path}")
    else:
        print(f"\nError: {resp.text[:2000]}")

except Exception as e:
    elapsed = time.time() - start
    print(f"请求失败 ({elapsed:.1f}s): {e}")
