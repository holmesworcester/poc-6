import base64

sig = "dummy_sig_bWVzc2FnZTptc2dfd2FpdGluZzpjaGFubmVsXzEyMzp1c2VyXzQ1NjpCbG9ja2VkIG1lc3NhZ2U=_by_user_456"

# Extract the base64 part
b64_part = sig.split("_")[2]
decoded = base64.b64decode(b64_part).decode('utf-8')
print(f"Decoded: {decoded}")
print(f"Expected: message:msg_waiting:channel_123:user_456:user_456:Blocked message")