# 🎒 Klee Bot - 可莉 QQ 机器人

基于 **Hermes Agent** + **NapCat** 的 QQ 群聊 AI 机器人，扮演《原神》火花骑士可莉。

## ✨ 特性

- 🧠 **AI 对话** — Hermes Agent + DeepSeek 驱动
- 🎨 **AI 画图** — 即梦 API (Jimeng) 生成可莉图片
- 📚 **原神知识库** — 125 角色数据 + 游戏模式速查
- 😊 **表情包** — meme_manager + meme_generator
- 🗣️ **智能回复** — 主动回复/兴趣衰减/对话感知/睡眠
- 🔍 **图片理解** — qwen-vl-plus 视觉模型
- 🛠️ **MCP 工具** — 19 个可扩展工具
- ⏰ **时间感知** — 活跃/安静/睡眠时段
- 💾 **记忆系统** — 跨会话持久记忆
- 🎭 **人格系统** — SOUL.md 可莉角色设定

## 📁 项目结构

```
klee-bot/
├── hermes/          # Hermes Agent 配置
│   ├── SOUL.md      # 可莉人格
│   ├── config.yaml.example
│   └── MEMORY.md
├── napcat/          # NapCat Docker
│   └── docker-compose.yml
├── mcp/             # MCP 工具
│   ├── klee_mcp_server.py
│   └── command_filter.json
└── knowledge/       # 知识库
    └── genshin.md
```

## 🚀 快速部署

```bash
# 1. 安装
pip install hermes-agent hermes-napcat

# 2. 配置
cp hermes/config.yaml.example ~/.hermes/config.yaml
nano ~/.hermes/config.yaml  # 填入 API Key
cp hermes/SOUL.md ~/.hermes/

# 3. 启动 NapCat
cd napcat && docker compose up -d

# 4. 启动 Hermes
hermes gateway

# 5. 启动 MCP
cd mcp && python klee_mcp_server.py &
```
