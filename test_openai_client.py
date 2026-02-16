#!/usr/bin/env python3
"""
Test Chat2API with OpenAI client library
"""

from openai import OpenAI

# Point to local Chat2API server
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="dummy"  # Not used, but required by OpenAI client
)

print("🧪 Testing Chat2API with OpenAI Client\n")
print("=" * 60)

# Test 1: Simple conversation
print("\n📝 Test 1: Simple Conversation")
print("-" * 60)
response = client.chat.completions.create(
    model="pplx-pro",
    messages=[
        {"role": "user", "content": "What is the capital of France? Answer in one sentence."}
    ]
)
print(f"Q: What is the capital of France?")
print(f"A: {response.choices[0].message.content}")

# Test 2: Programming question
print("\n💻 Test 2: Programming Question")
print("-" * 60)
response = client.chat.completions.create(
    model="pplx-pro",
    messages=[
        {"role": "user", "content": "Write a Python function to calculate fibonacci numbers. Keep it simple and short."}
    ]
)
print(f"Q: Write a Python function to calculate fibonacci numbers")
print(f"A:\n{response.choices[0].message.content}")

# Test 3: Streaming conversation
print("\n🌊 Test 3: Streaming Response")
print("-" * 60)
print("Q: Explain what is machine learning in 2 sentences")
print("A: ", end='', flush=True)

stream = client.chat.completions.create(
    model="pplx-pro",
    messages=[
        {"role": "user", "content": "Explain what is machine learning in 2 sentences"}
    ],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end='', flush=True)

print("\n")
print("=" * 60)
print("\n✅ All tests completed successfully!")
print("\n💡 Chat2API is working perfectly with OpenAI client!")
