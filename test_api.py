#!/usr/bin/env python3
"""
Test script for Chat2API server
"""

import requests
import json

BASE_URL = "http://localhost:8000"

def test_health():
    """Test health endpoint"""
    print("\n🔍 Testing /health endpoint...")
    response = requests.get(f"{BASE_URL}/health")
    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    return response.status_code == 200

def test_models():
    """Test models endpoint"""
    print("\n🔍 Testing /v1/models endpoint...")
    response = requests.get(f"{BASE_URL}/v1/models")
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Models: {[m['id'] for m in data['data']]}")
    return response.status_code == 200

def test_chat_completion_non_streaming():
    """Test non-streaming chat completion"""
    print("\n🔍 Testing /v1/chat/completions (non-streaming)...")
    
    payload = {
        "model": "pplx-pro",
        "messages": [
            {"role": "user", "content": "What is 2+2? Answer in one short sentence."}
        ],
        "stream": False
    }
    
    response = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json=payload,
        headers={"Content-Type": "application/json"}
    )
    
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        answer = data['choices'][0]['message']['content']
        print(f"Answer: {answer}")
        return True
    else:
        print(f"Error: {response.text}")
        return False

def test_chat_completion_streaming():
    """Test streaming chat completion"""
    print("\n🔍 Testing /v1/chat/completions (streaming)...")
    
    payload = {
        "model": "pplx-pro",
        "messages": [
            {"role": "user", "content": "What is the capital of Japan? Answer in one sentence."}
        ],
        "stream": True
    }
    
    response = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json=payload,
        headers={"Content-Type": "application/json"},
        stream=True
    )
    
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        print("Streaming response: ", end='', flush=True)
        for line in response.iter_lines():
            if line:
                line_str = line.decode('utf-8')
                if line_str.startswith('data: '):
                    data_str = line_str[6:]
                    if data_str == '[DONE]':
                        break
                    try:
                        data = json.loads(data_str)
                        if 'choices' in data and len(data['choices']) > 0:
                            delta = data['choices'][0].get('delta', {})
                            content = delta.get('content', '')
                            if content:
                                print(content, end='', flush=True)
                    except json.JSONDecodeError:
                        pass
        print()
        return True
    else:
        print(f"Error: {response.text}")
        return False

def main():
    print("🚀 Chat2API Server Test Suite\n")
    print("=" * 60)
    
    tests = [
        ("Health Check", test_health),
        ("Models List", test_models),
        ("Chat Completion (Non-Streaming)", test_chat_completion_non_streaming),
        ("Chat Completion (Streaming)", test_chat_completion_streaming),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            print(f"❌ Error: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 60)
    print("\n📊 Test Results:\n")
    for name, success in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} - {name}")
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
    else:
        print("\n⚠️  Some tests failed")

if __name__ == "__main__":
    main()
