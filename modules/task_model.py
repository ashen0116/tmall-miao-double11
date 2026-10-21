"""Pure task recognition shared by accessibility trees and local OCR."""
from dataclasses import asdict, dataclass
import hashlib
import re
import unicodedata


def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)))


def bounds_of(value):
    if isinstance(value, dict):
        return tuple(int(value.get(k, 0)) for k in ("left", "top", "right", "bottom"))
    if isinstance(value, (tuple, list)) and len(value) == 4:
        return tuple(map(int, value))
    numbers = re.findall(r"-?\d+", str(value or ""))
    return tuple(map(int, numbers[:4])) if len(numbers) >= 4 else (0, 0, 0, 0)


@dataclass
class TextNode:
    text: str
    bounds: tuple
    source: str = "ui"

    @property
    def center(self):
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass
class Task:
    key: str
    title: str
    detail: str
    action: str
    bounds: tuple
    current: int = 0
    total: int = 1
    seconds: int = 0
    kind: str = "browse"
    skip_reason: str = ""
    target_app: str = ""
    target_packages: tuple = ()

    def to_dict(self):
        return asdict(self)


ACTION_RE = re.compile(r"^(?:去完成|去浏览|去逛逛|去看看|去搜索|去观看|去参与|去领取|领取奖励|立即领取|领能量|领体力|领奖励|已完成|已领取|明日再来|去签到)$")
CLAIM_ACTIONS = {"去领取", "领取奖励", "立即领取", "领能量", "领体力", "领奖励"}
DONE_ACTIONS = {"已完成", "已领取", "明日再来"}
PROGRESS_RE = re.compile(r"[([]\s*(\d+)\s*/\s*(\d+)\s*[)\]]")
UNSUPPORTED_RE = re.compile(r"下单|购买|支付\d|支付订单|充值|开通|订阅|邀请|助力|组队|分享|加购|收藏|关注|授权|登录|登陆|实名|下载|安装|答题|考试|拍照|拍摄|发布|评价|种树|喂鸡|浇水")
EXCLUDED_RE = re.compile(r"游戏|拉人|拉\d+人|邀请|助力|组队|考试|答题")
DRAW_RE = re.compile(r"抽奖|夺宝|抽\d+元红包")
# Map explicit app names and known Alipay mini-app brands; internal channels
# such as 淘宝闪购 remain usable without installing another application.
EXTERNAL_APPS = (
    ('快手极速版', ('com.kuaishou.nebula',)),
    ('快手', ('com.smile.gifmaker', 'com.kuaishou.nebula')),
    ('微博', ('com.sina.weibo', 'com.sina.weibolite')),
    ('抖音', ('com.ss.android.ugc.aweme', 'com.ss.android.ugc.aweme.lite')),
    ('支付宝', ('com.eg.android.AlipayGphone',)),
    ('京东', ('com.jingdong.app.mall',)),
    ('拼多多', ('com.xunmeng.pinduoduo',)),
    ('闲鱼', ('com.taobao.idlefish',)),
    ('百度', ('com.baidu.searchbox',)),
    ('菜鸟', ('com.cainiao.wireless',)),
    ('高德', ('com.autonavi.minimap',)),
    ('优酷', ('com.youku.phone',)),
)


def external_app_names():
    """Distinct external app names offered as cross-app allowlist choices."""
    return list(dict.fromkeys(name for name, _ in EXTERNAL_APPS))


def parse_tasks(nodes, width=1440, height=3200):
    """Pair right-hand action buttons with text in the same visual row.

    Geometry handles Themis pages whose accessibility siblings are unrelated,
    and works identically when the page exposes only a bitmap.
    """
    actions = []
    # A tablet/landscape panel letterboxes a portrait Taobao window, so the
    # right-hand action column no longer sits beyond 55% of the full width.
    right_edge = width * (.45 if width > height else .55)
    for node in nodes:
        label = normalize(node.text)
        x, y = node.center
        if ACTION_RE.fullmatch(label) and x > right_edge and 0 < y < height * .985:
            if not any(abs(y - old.center[1]) < height * .012 for old in actions):
                actions.append(node)
    actions.sort(key=lambda n: n.center[1])
    tasks = []
    for idx, button in enumerate(actions):
        y = button.center[1]
        prev = actions[idx - 1].center[1] if idx else y - height * .16
        nxt = actions[idx + 1].center[1] if idx + 1 < len(actions) else y + height * .16
        top = max((prev + y) / 2, y - height * .055)
        bottom = min((nxt + y) / 2, y + height * .042)
        row = []
        for n in nodes:
            x0, y0 = n.center
            if top <= y0 <= bottom and n.bounds[0] < button.bounds[0] - width * .025:
                text = normalize(n.text)
                if text and not ACTION_RE.fullmatch(text) and n.bounds[3] - n.bounds[1] < height * .1:
                    row.append(n)
        row.sort(key=lambda n: (round(n.center[1] / (height * .008)), n.bounds[0]))
        texts = list(dict.fromkeys(normalize(n.text) for n in row
                                   if not re.search(r'O1CN|!!\d+|\.(?:png|jpg|webp)|^\W+$', n.text)))
        # Do not guess what an unlabelled button does.
        if not texts:
            continue
        title = next((t for t in texts if PROGRESS_RE.search(t)), texts[0])
        if title in {"累计任务奖励", "当前进度", "音效"}:
            continue
        detail = " ".join(t for t in texts if t != title)
        progress = PROGRESS_RE.search(title)
        # During reward animations OCR can read a toast over a task's title.
        # Wait for a real progress label instead of treating that text as a task.
        if not progress:
            continue
        current, total = map(int, progress.groups()) if progress else (0, 1)
        # Some WebViews expose the modal's complete row in one node while
        # continuing to expose the obscured homepage underneath. Once this
        # node already includes its description, nearby background text must
        # not contribute payment/game keywords or app names to this task.
        suffix = title[progress.end():].strip()
        if suffix and row and any(n.source == 'ui' and normalize(n.text) == title for n in row):
            texts = [title]
            detail = suffix
            title = title[:progress.start()].strip()
        else:
            title = PROGRESS_RE.sub("", title).strip()
        # Themis sometimes exposes a whole row as one accessibility string,
        # whereas OCR separates the title and description into two lines.
        description = re.search(r'(?:下滑)?浏览\d+[秒sS]|点击去逛|跳转得(?:奖励)?|玩\d+款游戏得|邀请好友成功|去答题最高得|去答题得', title)
        if description:
            suffix = title[description.start():]
            title = title[:description.start()].strip()
            if suffix not in detail:
                detail = (detail + ' ' + suffix).strip()
        if not title:
            continue
        combined = normalize(' '.join(texts))
        duration = re.search(r"(?:浏览|停留|观看|逛|滑动|下滑)[^\d]{0,5}(\d{1,3})[秒sS]", combined)
        seconds = int(duration.group(1)) if duration else 0
        kind, reason = "browse", ""
        action = normalize(button.text)
        target_app, packages = next(((name, packages) for name, packages in EXTERNAL_APPS
                                     if name in title), ('', ()))
        if not target_app and re.search(r'蚂蚁庄园|蚂蚁森林', title):
            target_app, packages = '支付宝', ('com.eg.android.AlipayGphone',)
        flash_visit = (title == '领闪购红包下单直接省' and '点击去逛' in combined
                       and not re.search(r'需下单|完成下单|支付|购买|消费|订单', detail))
        passive_app_visit = bool(target_app and re.search(r'跳转得(?:奖励)?|点击去逛', detail))
        safety_text = combined.replace('领闪购红包下单直接省', '领闪购红包', 1) if flash_visit else combined
        if action in DONE_ACTIONS or current >= total:
            kind, reason = "done", "今日已完成"
        elif EXCLUDED_RE.search(combined) or (DRAW_RE.search(combined) and not passive_app_visit):
            kind, reason = "manual", "已跳过：需要游戏、拉人、抽奖或答题考试操作"
        elif UNSUPPORTED_RE.search(safety_text):
            kind, reason = "manual", "需要手动操作：" + UNSUPPORTED_RE.search(safety_text).group()
        elif re.search(r"浏览\d+个|逛\d+个|\d+件商品", combined):
            kind, reason = "manual", "需要多商品交互"
        elif action in CLAIM_ACTIONS:
            kind = "claim"
        elif flash_visit or target_app:
            kind = "visit"
        elif re.search(r"搜一搜|搜索", combined):
            kind = "search"
        elif re.search(r"支付宝|蚂蚁庄园|蚂蚁森林|农场|菜鸟|闲鱼|饿了么|优酷|高德|淘宝闪购|天猫|去逛", combined):
            kind = "visit"
        elif not re.search(r"浏览|逛|看|签到|打卡|频道|会场", combined):
            kind, reason = "manual", "任务类型暂不支持"
        key = hashlib.sha256(normalize(title).encode("utf-8")).hexdigest()[:16]
        tasks.append(Task(key, title, detail, action, button.bounds, current, total,
                          seconds, kind, reason, target_app, packages))
    # Cumulative rewards live above the list and are not right-hand row buttons.
    header = next((n for n in nodes if normalize(n.text) == '累计任务奖励'), None)
    if header:
        reward_bottom = min((t.bounds[1] for t in tasks), default=height)
        reward_keys = set()
        for n in nodes:
            label = normalize(n.text)
            if (re.search(r'立即领取|领取奖励|点击领取', label)
                    and n.bounds[1] >= header.bounds[1] - height * .01
                    and n.bounds[3] < reward_bottom
                    and n.bounds[3] - n.bounds[1] < height * .1
                    and n.bounds[2] - n.bounds[0] < width * .5):
                key = f'milestone-{round(n.center[0] / width * 10)}'
                if key not in reward_keys:
                    reward_keys.add(key)
                    tasks.append(Task(key, '领取累计任务奖励', label, '立即领取', n.bounds, kind='claim'))
    return tasks


def is_task_list(nodes):
    text = normalize(" ".join(n.text for n in nodes))
    return ("累计任务奖励" in text or
            bool(re.search(r"做任务赚|任务列表|赚能量任务|任务中心", text))) and bool(
                re.search(r"去完成|去浏览|去逛逛|去看看|已完成|已领取|领取奖励|立即领取|明日再来", text))
