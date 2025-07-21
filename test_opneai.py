import os
import openai

# 環境変数から読み込む
openai.api_key = os.getenv("OPENAI_API_KEY")

# 動作テスト
resp = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[{"role":"user","content":"Hello"}]
)

print(resp.choices[0].message.content)
