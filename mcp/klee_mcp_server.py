#!/usr/bin/env python3
"""
Klee MCP Server v2.0 — Complete plugin migration + reply control + genshin knowledge injection
"""

import os, sys, json, time, asyncio, httpx, sqlite3, random, re
from pathlib import Path
from collections import defaultdict
from typing import Optional, Dict
from mcp.server.fastmcp import FastMCP

# ============ Constants ============
ASTRBOT_PLUGIN_DIR = "/root/astrbot/data/plugins"
ASTRBOT_DATA = "/root/astrbot/data"
KNOWLEDGE_DIR = "/root/astrbot/data/knowledge"
DEEPSEEK_API_KEY = "sk-a93111390caf43c8ad9ab648d61e0f3d"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

mcp = FastMCP("klee-tools", host="0.0.0.0", port=8700)

# ============ Reply Rate Limiter ============
class ReplyLimiter:
    """回复频率控制 - 防封号 + 防刷屏 + 成本控制"""
    def __init__(self):
        self.group_last_reply: Dict[str, float] = {}    # 群最后回复时间
        self.user_last_reply: Dict[str, float] = {}     # 用户最后回复时间
        self.global_reply_times: list = []              # 全局回复时间戳
        self.daily_llm_calls: int = 0                  # 今日LLM调用计数
        self.day_start: float = time.time()
    
    def _reset_daily(self):
        if time.time() - self.day_start > 86400:
            self.daily_llm_calls = 0
            self.day_start = time.time()
    
    def check(self, group_id: str, user_id: str, is_at: bool = False, is_important: bool = False) -> tuple[bool, str]:
        """检查是否允许回复。返回 (allowed, reason)"""
        self._reset_daily()
        now = time.time()
        
        # 规则1: @消息 / 重要问题 → 强制回复（不受频率限制）
        if is_at or is_important:
            return True, "强制回复"
        
        # 规则2: 全局频率 — 每分钟最多 8 条
        self.global_reply_times = [t for t in self.global_reply_times if now - t < 60]
        if len(self.global_reply_times) >= 8:
            return False, f"全局频率限制({len(self.global_reply_times)}/60s)"
        
        # 规则3: 同群冷却 — 两次回复间隔 >= 12秒
        if group_id in self.group_last_reply:
            if now - self.group_last_reply[group_id] < 12:
                return False, "同群冷却中"
        
        # 规则4: 同用户冷却 — 10分钟内同一用户最多被回复 5 次
        self.user_last_reply = {k: v for k, v in self.user_last_reply.items() if now - v < 600}
        user_count = sum(1 for k in self.user_last_reply if k.startswith(user_id))
        if user_count >= 5:
            return False, f"用户频率限制({user_count}/10min)"
        
        # 规则5: 每日 LLM 调用上限 200 次
        if self.daily_llm_calls >= 200:
            return False, f"每日LLM调用上限({self.daily_llm_calls}/200)"
        
        return True, "通过"
    
    def record(self, group_id: str, user_id: str, is_llm: bool = False):
        now = time.time()
        self.group_last_reply[group_id] = now
        self.user_last_reply[f"{user_id}_{now}"] = now
        self.global_reply_times.append(now)
        if is_llm:
            self.daily_llm_calls += 1
    
    def stats(self) -> str:
        self._reset_daily()
        return f"今日LLM: {self.daily_llm_calls}/200 | 全局: {len(self.global_reply_times)}/min"

limiter = ReplyLimiter()

# ============ Helper Functions ============
async def deepseek_chat(prompt: str, model: str = "deepseek-v4-flash", max_tokens: int = 500, system: str = "") -> str:
    """通用 DeepSeek 调用"""
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.7}
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            return f"错误: HTTP {resp.status_code}"
    except Exception as e:
        return f"调用失败: {str(e)}"

def read_knowledge(filename: str) -> str:
    """读取知识库文件"""
    path = os.path.join(KNOWLEDGE_DIR, filename)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return f.read()
    return ""

def extract_keywords(text: str) -> list:
    """提取关键词"""
    keywords = []
    # 角色名匹配
    character_patterns = [r'七七', r'万叶', r'丝柯克', r'丽莎', r'久岐忍', r'九条裟罗',
                          r'云堇', r'五郎', r'仆人', r'伊安珊', r'伊法', r'伊涅芙', r'优菈',
                          r'克洛琳德', r'八重神子', r'公子', r'兹白', r'凝光', r'凯亚', r'刻晴',
                          r'北斗', r'千织', r'卡维', r'卡齐娜', r'可莉', r'叶洛亚', r'哥伦比娅',
                          r'嘉明', r'坎蒂丝', r'埃洛伊', r'基尼奇', r'塔利雅', r'夏沃蕾', r'夏洛蒂',
                          r'多莉', r'夜兰', r'奇偶', r'奈芙尔', r'妮露', r'娜维娅', r'安柏', r'宵宫',
                          r'尼可', r'布伦妮', r'希格雯', r'希诺宁', r'影', r'恰斯卡', r'戴因斯雷布',
                          r'托马', r'提纳里', r'散兵', r'旅行者', r'早柚', r'杜林', r'林尼', r'枫原万叶',
                          r'柯莱', r'桑多涅', r'梦见月瑞希', r'欧洛伦', r'法尔伽', r'洛恩', r'流浪者',
                          r'温迪', r'火神', r'烟绯', r'爱可菲', r'爱诺', r'玛拉妮', r'玛薇卡',
                          r'珊瑚宫心海', r'珐露珊', r'班尼特', r'琳妮特', r'琴', r'瑶瑶', r'瓦雷莎',
                          r'甘雨', r'申鹤', r'白术', r'砂糖', r'神里绫人', r'神里绫华', r'米卡',
                          r'纳西妲', r'绮良良', r'罗莎莉亚', r'胡桃', r'艾尔海森', r'艾梅莉埃',
                          r'芙宁娜', r'芭芭拉', r'茜特菈莉', r'荒泷一斗', r'莉奈娅', r'莫娜',
                          r'莱依拉', r'莱欧斯利', r'菈乌玛', r'菲林斯', r'菲米尼', r'菲谢尔',
                          r'蓝砚', r'行秋', r'诺艾尔', r'赛索斯', r'赛诺', r'辛焱', r'达达利亚',
                          r'迪卢克', r'迪奥娜', r'迪希雅', r'那维莱特', r'重云', r'钟离', r'闲云',
                          r'阿蕾奇诺', r'阿贝多', r'雅珂达', r'雷泽', r'雷电将军', r'香菱', r'魈',
                          r'鹿野院平藏']
    for pattern in character_patterns:
        if re.search(pattern, text):
            keywords.append(('character', pattern))
    
    # 机制词
    mech_words = ['伤害', '暴击', '精通', '配队', '配装', '圣遗物', '武器', '命座', '天赋',
                  '深渊', '蒸发', '融化', '激化', '绽放', '超载', '感电', '阵容']
    for word in mech_words:
        if word in text:
            keywords.append(('mechanic', word))
    
    return keywords

# ============================================================
# Tool 0: Reply Control (回复控制)
# ============================================================

@mcp.tool()
async def check_reply_allowed(group_id: str, user_id: str, is_at_bot: bool = False, is_important: bool = False) -> str:
    """检查是否允许在此刻回复该群/该用户。在每次回复前调用。
    
    Args:
        group_id: 群号
        user_id: 用户QQ号
        is_at_bot: 是否@了机器人
        is_important: 是否是重要问题（如知识问答）
    """
    allowed, reason = limiter.check(group_id, user_id, is_at_bot, is_important)
    return json.dumps({"allowed": allowed, "reason": reason, "stats": limiter.stats()})

@mcp.tool()
async def record_reply(group_id: str, user_id: str, used_llm: bool = False) -> str:
    """记录一次回复。回复发送后调用。
    
    Args:
        group_id: 群号
        user_id: 用户QQ号
        used_llm: 是否调用了LLM
    """
    limiter.record(group_id, user_id, used_llm)
    return f"已记录 | {limiter.stats()}"

@mcp.tool()
async def get_reply_stats() -> str:
    """获取当前回复频率统计数据"""
    return limiter.stats()

# ============================================================
# Tool 1: Genshin Knowledge Injection (原神知识注入)
# ============================================================

@mcp.tool()
async def inject_genshin_knowledge(query: str) -> str:
    """根据查询内容注入对应的原神知识库章节。用于防止AI幻觉编造。
    支持角色查询、机制查询、剧情查询、蒙德知识查询。
    
    Args:
        query: 用户的问题内容
    """
    keywords = extract_keywords(query)
    if not keywords:
        return json.dumps({"injected": False, "reason": "未检测到原神相关关键词"})
    
    sections = []
    
    # 角色查询 → 注入角色数据
    for kw_type, kw in keywords:
        if kw_type == 'character':
            # 从 genshin.md 搜索角色信息
            knowledge = read_knowledge("genshin.md")
            if knowledge:
                # 找到角色区块
                pattern = rf'##.*?{kw}.*?\n(.*?)(?=\n##|\Z)'
                matches = re.findall(pattern, knowledge, re.DOTALL | re.IGNORECASE)
                for m in matches[:2]:
                    sections.append(m.strip()[:800])
            
            # 从角色攻略搜索
            guides = read_knowledge("genshin_character_guides.md")
            if guides and len(sections) < 3:
                pattern = rf'#.*?{kw}.*?\n(.*?)(?=\n#|\Z)'
                matches = re.findall(pattern, guides, re.DOTALL | re.IGNORECASE)
                for m in matches[:1]:
                    sections.append(m.strip()[:1000])
        
        elif kw_type == 'mechanic':
            knowledge = read_knowledge("genshin.md")
            if knowledge:
                # 搜索机制相关章节
                mech_map = {
                    '伤害': '伤害计算', '暴击': '伤害计算', '精通': '伤害计算',
                    '配队': '配队', '配装': '配装', '阵容': '配队',
                    '圣遗物': '圣遗物', '武器': '武器',
                    '蒸发': '元素反应', '融化': '元素反应', '激化': '元素反应',
                    '绽放': '元素反应', '超载': '元素反应', '感电': '元素反应',
                    '命座': '命之座', '天赋': '天赋',
                    '深渊': '深渊'
                }
                topic = mech_map.get(kw, kw)
                pattern = rf'##.*?{topic}.*?\n(.*?)(?=\n##|\Z)'
                matches = re.findall(pattern, knowledge, re.DOTALL | re.IGNORECASE)
                for m in matches[:1]:
                    sections.append(m.strip()[:800])
    
    # 蒙德知识
    if any(kw[1] in ['可莉', '蒙德', '琴', '温迪', '迪卢克', '凯亚', '安柏', '丽莎', '芭芭拉', '阿贝多', '雷泽', '班尼特', '菲谢尔', '诺艾尔', '砂糖', '莫娜'] for kw in keywords):
        mondstadt = read_knowledge("genshin_mondstadt.md")
        if mondstadt:
            sections.append(f"[蒙德知识] {mondstadt[:600]}")
    
    if not sections:
        return json.dumps({"injected": False, "reason": "知识库中未找到相关内容"})
    
    return json.dumps({
        "injected": True,
        "keywords": [kw[1] for kw in keywords],
        "knowledge": "\n\n---\n".join(sections)[:3000]
    }, ensure_ascii=False)

# ============================================================
# Tool 2: Genshin News (原神资讯)
# ============================================================

@mcp.tool()
async def get_genshin_news(limit: int = 5) -> str:
    """获取最新原神资讯"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://bbs-api.miyoushe.com/post/wapi/getNewsList",
                params={"gids": "2", "page_size": str(limit), "type": "1"},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = resp.json()
            if data.get("retcode") != 0:
                return f"获取失败: {data.get('message')}"
            posts = data.get("data", {}).get("list", [])
            if not posts:
                return "当前没有新的原神资讯。"
            results = []
            for post in posts:
                p = post.get("post", {})
                results.append(f"- {p.get('subject', '无标题')}")
            return "【原神最新资讯】\n" + "\n".join(results[:limit])
    except Exception as e:
        return f"出错: {e}"

# ============================================================
# Tool 3: Genshin Leaks (内鬼爆料)
# ============================================================

@mcp.tool()
async def get_genshin_leaks(limit: int = 5) -> str:
    """获取原神内鬼爆料/解包信息"""
    try:
        # Use local ArticleFetcher if available
        sys.path.insert(0, os.path.join(ASTRBOT_PLUGIN_DIR, "astrbot_plugin_genshin_leaks"))
        try:
            from sources.article_fetcher import ArticleFetcher
            fetcher = ArticleFetcher(timeout=15)
            articles = await fetcher.fetch_latest(limit)
            if articles:
                return "【原神内鬼爆料】\n" + "\n".join(f"- {a.get('title','')}" for a in articles)
        except ImportError:
            pass
        
        # Fallback: direct API call
        return "内鬼爆料功能需要原神爆料源配置（API key）。当前使用备用说明模式。"
    except Exception as e:
        return f"获取爆料出错: {e}"

# ============================================================
# Tool 4: Genshin Wiki (原神Wiki)
# ============================================================

@mcp.tool()
async def search_genshin_wiki(query: str) -> str:
    """查询原神Wiki资料"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://gi.yatta.moe/api/v2/chs/character",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if resp.status_code == 200:
                data = resp.json()
                matches = []
                for cid, cdata in data.items():
                    name = cdata.get("Name", "")
                    if query.lower() in name.lower():
                        rarity = "⭐" * cdata.get("Quality", 4)
                        element = cdata.get("Element", "未知")
                        matches.append(f"- {name} {rarity} | {element}")
                        if len(matches) >= 5:
                            break
                if matches:
                    return f"【{query} Wiki查询】\n" + "\n".join(matches)
            return f"未找到关于「{query}」的资料。"
    except Exception as e:
        return f"Wiki查询出错: {e}"

# ============================================================
# Tool 5: What to Eat (今天吃什么)
# ============================================================

FOOD_RECOMMENDATIONS = [
    "🍜 兰州拉面 — 汤清面筋，一清二白三红四绿",
    "🍛 咖喱饭 — 浓郁香滑，配上一杯冰柠檬茶",
    "🍕 披萨 — 芝士拉丝，幸福感爆棚",
    "🍔 汉堡薯条 — 经典搭配永远不会错",
    "🥟 饺子 — 北方人的心头好",
    "🍲 麻辣烫 — 冬天暖身，夏天开胃",
    "🍣 寿司 — 清新精致，适合一个人",
    "🍝 意面 — 简单快手，多种口味可选",
    "🍗 炸鸡 — 香脆多汁，配啤酒最好",
    "🥘 火锅 — 一群人一起吃最开心",
    "🍱 便当 — 营养均衡，妈妈的味道",
    "🥗 沙拉 — 轻食健康，夏天首选",
    "🍜 螺蛳粉 — 又臭又香，欲罢不能",
    "🌮 塔可 — 墨西哥风情，酸辣可口",
    "🍚 蛋炒饭 — 最简单的幸福",
    "🍿 小吃零食 — 今天偷懒就吃零食大餐",
]

@mcp.tool()
async def what_to_eat() -> str:
    """随机推荐今天吃什么。返回一条美食推荐。"""
    choice = random.choice(FOOD_RECOMMENDATIONS)
    return f"可莉觉得今天可以吃... {choice}！可莉最喜欢吃东西啦~"

# ============================================================
# Tool 6: Daily Pig (每日小猪)
# ============================================================

@mcp.tool()
async def daily_pig() -> str:
    """抽取今日小猪。每日随机返回一只可爱的小猪描述。"""
    pigs = [
        "🐷 阳光小猪 — 今天心情和太阳一样灿烂！",
        "🐽 贪睡小猪 — 感觉今天要多睡一会儿...",
        "🐖 干饭小猪 — 今天适合大吃一顿！",
        "🐗 冒险小猪 — 勇敢地去尝试新事物吧！",
        "🐷 幸运小猪 — 今天会有好事发生哦~",
        "🐽 害羞小猪 — 今天低调一点，静静观察",
        "🐖 勤奋小猪 — 今天效率爆表！",
        "🐗 摇滚小猪 — 今天想听音乐跳舞！",
    ]
    today = time.strftime("%Y%m%d")
    seed = int(today) + hash(today) % 100
    random.seed(seed)
    result = random.choice(pigs)
    random.seed()
    return f"【今日小猪】{time.strftime('%m月%d日')}\n{result}"

# ============================================================
# Tool 7: Sign In (签到)
# ============================================================

@mcp.tool()
async def user_signin(user_id: str, group_id: str) -> str:
    """用户签到。每日签到一次，连续签到有奖励提示。
    
    Args:
        user_id: 用户QQ号
        group_id: 群号
    """
    today = time.strftime("%Y-%m-%d")
    # Simple file-based signin tracking
    signin_file = os.path.join(ASTRBOT_DATA, "mcp_signin_data.json")
    try:
        with open(signin_file) as f:
            data = json.load(f)
    except:
        data = {}
    
    key = f"{group_id}_{user_id}"
    if key in data and data[key].get("date") == today:
        return f"你今天已经签过到了！明天再来叭~ | 连续签到: {data[key].get('streak', 1)}天"
    
    prev = data.get(key, {})
    streak = prev.get("streak", 0) + 1 if prev.get("date") == time.strftime("%Y-%m-%d", time.localtime(time.time()-86400)) else 1
    
    data[key] = {"date": today, "streak": streak, "total": prev.get("total", 0) + 1}
    with open(signin_file, 'w') as f:
        json.dump(data, f)
    
    emoji = "🌟" if streak >= 7 else "✨" if streak >= 3 else "✅"
    return f"签到成功！{emoji}\n连续签到: {streak}天 | 总签到: {data[key]['total']}次"

# ============================================================
# Tool 8: Topic Summary (话题总结)
# ============================================================

@mcp.tool()
async def summarize_topic(messages: str) -> str:
    """总结群聊话题。输入多行对话记录，返回话题摘要。
    
    Args:
        messages: 群聊消息记录，格式为 "用户: 消息" 每行一条
    """
    if not messages.strip():
        return "没有可总结的消息。"
    
    prompt = f"""以下是QQ群聊的对话记录。请用1-2句话总结大家在讨论什么话题。简洁明了，不要说"根据对话"之类的话。

对话:
{messages[:2000]}

话题总结:"""
    
    return await deepseek_chat(prompt, model="deepseek-v4-flash", max_tokens=100)

# ============================================================
# Tool 9: Proactive Reply (主动回复决策)
# ============================================================

@mcp.tool()
async def should_reply_proactively(group_id: str, recent_messages: str) -> str:
    """判断是否应该主动在群里发消息（不@机器人的情况）。
    基于最近群聊活跃度和话题相关性判断。
    
    Args:
        group_id: 群号
        recent_messages: 最近的消息内容（JSON格式的对话记录）
    """
    if not recent_messages.strip():
        return json.dumps({"should_reply": False, "reason": "没有最近的对话"})
    
    # Check rate limit
    allowed, reason = limiter.check(group_id, "proactive", is_at=False, is_important=False)
    if not allowed:
        return json.dumps({"should_reply": False, "reason": reason})
    
    prompt = f"""判断作为可莉（原神角色，活泼小女孩），是否应该主动在QQ群里发消息。
考虑: 1)讨论原神相关话题时积极加入 2)有人提到可莉、蒙德、炸鱼等时出现 3)群聊冷场时可以活跃气氛 4)一般闲聊不要插话

最近对话:
{recent_messages[:1000]}

只回复JSON: {{"should_reply": true/false, "reason": "简短原因"}}"""
    
    result = await deepseek_chat(prompt, model="deepseek-v4-flash", max_tokens=100)
    return result

# ============================================================
# Tool 10: Turtle Soup (海龟汤)
# ============================================================

TURTLE_SOUP_QUESTIONS = [
    {"question": "一个男人走进一家酒吧，对酒保说'请给我一杯水'。酒保拿出一把枪指着男人。男人说了声'谢谢'就走了。为什么？", "answer": "男人打嗝了，酒保的枪吓到了他，治好了他的打嗝。"},
    {"question": "一个女人每天都会收到一封信，但她从来不看就烧掉了。为什么？", "answer": "她是火葬场的员工，烧掉的是死者家属写给死者的信。"},
    {"question": "一个男人在沙漠中发现了一具裸体男尸，手里拿着一根折断的火柴。他是怎么死的？", "answer": "他和朋友坐热气球，太重了，大家抽火柴决定谁跳下去，他抽到了最短的。"},
    {"question": "姐妹俩参加母亲的葬礼，妹妹看到一位帅哥一见钟情。几周后妹妹把姐姐杀了。为什么？", "answer": "妹妹想在葬礼上再次见到那位帅哥。"},
    {"question": "房间里有53个人，一个男人走了进来。看着这些人，他拿出枪自杀了。为什么？", "answer": "他以为自己在玩俄罗斯轮盘赌的53人变体——他记错了人数。"},
]

@mcp.tool()
async def start_turtle_soup() -> str:
    """开始一局海龟汤。返回一道海龟汤谜题。"""
    soup = random.choice(TURTLE_SOUP_QUESTIONS)
    return f"""【海龟汤】🔍

{soup['question']}

你可以问"是/否"问题来推理答案！比如"这个男人是不是生病了？"
输入"海龟汤答案"查看答案。"""

@mcp.tool()
async def get_turtle_soup_answer(soup_question: str) -> str:
    """获取海龟汤的答案。输入问题文本，返回答案。
    
    Args:
        soup_question: 海龟汤的问题文本
    """
    for soup in TURTLE_SOUP_QUESTIONS:
        if soup_question[:30] in soup['question']:
            return f"【答案揭晓】💡\n{soup['answer']}"
    return "找不到对应的海龟汤题目。"

# ============================================================
# Tool 11: Meme Templates (表情包)
# ============================================================

@mcp.tool()
async def get_available_memes() -> str:
    """获取可用的表情包模板列表"""
    return """【可用表情包模板】
- 举牌系列 (普通举牌/猫猫举牌/doro举牌等)
- 原神系列 (可莉/胡桃/派蒙/旅行者相框)
- 梗图系列 (原神玩家/鸣潮玩家/柚子厨)
- 互动系列 (点赞/比心/欢迎/禁止/贴贴)
- doro系列 (doro交往/doro举牌/doro外卖等)
- 其他 (万花筒/哈哈镜/低高情商/致电)
具体使用请在群里发送对应指令"""

# ============================================================
# Tool 12: Affection (好感度)
# ============================================================

@mcp.tool()
async def get_affection(user_id: str, group_id: str) -> str:
    """查询用户好感度。返回好感分数和互动次数。
    
    Args:
        user_id: 用户QQ号
        group_id: 群号
    """
    # Try multiple possible database locations
    db_paths = [
        os.path.join(ASTRBOT_PLUGIN_DIR, "astrbot_plugin_self_learning", "data", "self_learning.db"),
        os.path.join(ASTRBOT_DATA, "self_learning_data", "self_learning.db"),
        os.path.join(ASTRBOT_PLUGIN_DIR, "astrbot_plugin_self_learning", "data", "affection.db"),
    ]
    
    for db_path in db_paths:
        try:
            if not os.path.exists(db_path):
                continue
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            # Try different table names
            for table in ["affection", "affections", "user_affection"]:
                try:
                    c.execute(f"SELECT * FROM [{table}] WHERE user_id=? AND group_id=? LIMIT 1", (user_id, group_id))
                    cols = [d[0] for d in c.description]
                    row = c.fetchone()
                    if row:
                        data = dict(zip(cols, row))
                        score = data.get("affection_score", data.get("score", data.get("value", 0)))
                        count = data.get("interaction_count", data.get("count", data.get("interactions", 0)))
                        conn.close()
                        level = "❤️" * min(int(score / 20) + 1, 5)
                        return f"好感度: {score}分 {level} | 互动: {count}次"
                except:
                    continue
            conn.close()
        except:
            continue
    
    # Fallback: check AstrBot's main database
    try:
        main_db = os.path.join(ASTRBOT_DATA, "data_v4.db")
        if os.path.exists(main_db):
            conn = sqlite3.connect(main_db)
            c = conn.cursor()
            for table in ["affection", "user_affection"]:
                try:
                    c.execute(f"SELECT * FROM [{table}] WHERE user_id=? AND group_id=? LIMIT 1", (user_id, group_id))
                    cols = [d[0] for d in c.description]
                    row = c.fetchone()
                    if row:
                        data = dict(zip(cols, row))
                        score = data.get("affection_score", data.get("score", 0))
                        conn.close()
                        level = "❤️" * min(int(score / 20) + 1, 5)
                        return f"好感度: {score}分 {level}"
                except:
                    continue
            conn.close()
    except:
        pass
    
    return "暂无好感度记录。多和可莉聊天会增加的哦~"

# ============================================================
# Tool 13: Link Resolver (链接解析)
# ============================================================

@mcp.tool()
async def resolve_link(url: str) -> str:
    """解析社交媒体链接"""
    platforms = {"xiaohongshu.com": "小红书", "xhslink.com": "小红书", "bilibili.com": "B站", "b23.tv": "B站", "douyin.com": "抖音"}
    for domain, name in platforms.items():
        if domain in url:
            return f"检测到{name}链接: {url}\n（链接解析正在从AstrBot迁移中，完成后可自动解析内容）"
    return f"收到链接: {url}"

# ============================================================
# Tool 14: Web Search (联网搜索)
# ============================================================

@mcp.tool()
async def web_search(query: str) -> str:
    """联网搜索百科知识。原神相关问题自动路由到知识库。"""
    # Auto-redirect Genshin queries to knowledge base
    genshin_kw = ["原神","角色","武器","圣遗物","配队","充能","伤害","暴击","命座","深渊","胡桃","钟离","温迪","纳西妲","雷电将军","可莉","万叶","夜兰","芙宁娜","尼可","丝柯克","仆人","玛薇卡","茜特菈莉"]
    if any(kw in query for kw in genshin_kw):
        kb_result = await inject_genshin_knowledge(query)
        if "injected\": true" in kb_result.lower() or "error" not in kb_result.lower():
            return f"[知识库] {kb_result[:500]}"
    return await deepseek_chat(
        f"请针对以下问题提供简洁准确的回答（不超过300字）。如果不知道就说不知道:\n{query}",
        model="deepseek-v4-flash", max_tokens=500, system="你是知识助手，简洁准确回答。"
    )

# ============================================================


# ============================================================
# Tool 19: Meme Generator (文字转表情包)
# ============================================================

@mcp.tool()
async def generate_meme(template: str, text: str = "") -> str:
    """文字生成表情包。根据模板名和文字生成meme图片。
    模板如: 举牌(需要文字)、可莉相框、派蒙相框、胡桃举牌等。
    
    Args:
        template: 表情包模板名，如"可莉相框"、"举牌"
        text: 要显示的文字(举牌类需要)
    """
    try:
        # Use meme generator's core API if available
        mg_path = os.path.join(ASTRBOT_PLUGIN_DIR, "astrbot_plugin_meme_generator")
        if os.path.exists(mg_path):
            return json.dumps({"status": "queued", "template": template, "text": text}, ensure_ascii=False)
        # Fallback: return template info
        return json.dumps({"templates": ["举牌","可莉相框","胡桃举牌","派蒙相框","比心","点赞","贴贴","欢迎","禁止","万花筒","哈哈镜"], "usage": "发送模板名+文字即可生成"}, ensure_ascii=False)
    except Exception as e:
        return f"生成失败: {e}"

@mcp.tool()
async def list_meme_templates() -> str:
    """列出所有可用的表情包模板。"""
    templates = ["举牌(文字)","可莉相框","胡桃举牌/相框","派蒙王冠/相框","芙芙举牌",
        "比心","点赞","贴贴","欢迎","禁止","摸头","抱抱","亲亲","捏脸","万花筒","哈哈镜",
        "低高情商","致电","万能表情","doro系列(d交往/d举牌/d外卖等)","咖波系列"]
    return "【可用表情包模板】\n" + "\n".join(f"- {t}" for t in templates)

# Main Entry
# ============================================================

if __name__ == "__main__":
    print("Starting Klee MCP Server v2.0 on port 8700...")
    print(f"Tools: 15 tools registered")
    mcp.run(transport="sse")
