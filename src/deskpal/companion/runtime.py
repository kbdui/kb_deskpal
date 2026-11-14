"""开放角色卡与五层状态。含义由模型产生，程序只落实归属、期限和修订。"""

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import RLock
import time
import math
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .errors import (CompanionClosedError as CompanionClosedError, CompanionError as CompanionError,
                     CompanionRevisionConflict as CompanionRevisionConflict,
                     CompanionStorageError as CompanionStorageError,
                     CompanionValidationError as CompanionValidationError)
from .repository import RoleRepository, encode, initial

LAYERS = ("user_understanding", "interaction_approach")
SCOPES = ("turn", "conversation", "day", "persistent")


def _text(value: Any, limit: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or (not empty and not value.strip())
            or len(value.encode("utf-8")) > limit or "\x00" in value):
        raise CompanionValidationError("companion_text_invalid")
    return value.strip()


class CompanionRuntime:
    def __init__(self, db_path: Path | str | None = None, *, timezone: str = "Asia/Shanghai",
                 wall_clock: Callable[[], float] | None = None,
                 monotonic_clock: Callable[[], float] | None = None) -> None:
        self._zone = ZoneInfo(timezone)
        self._clock = wall_clock or time.time
        self._lock = RLock()
        self._closed = False
        self._repository = RoleRepository(db_path, validator=self._validate)
        self._validate(self._repository.load())

    def _load(self) -> dict[str, Any]:
        if self._closed:
            raise CompanionClosedError("companion_closed")
        value = self._repository.load()
        self._validate(value)
        return value

    @staticmethod
    def _validate(value: dict[str, Any]) -> None:
        card = value["card"]
        if (set(card) != {"id", "revision", "text", "use_policy", "origin"}
                or not isinstance(card["id"], str) or len(card["id"]) != 32
                or type(card["revision"]) is not int or card["revision"] < 1
                or card["use_policy"] not in ("local_only", "cloud_allowed")
                or card["origin"] not in ("default", "user", "legacy_import")):
            raise CompanionStorageError("companion_state_invalid")
        _text(card["text"], 8192)
        for layer in (*LAYERS, "habits"):
            if len(value[layer]) > 64:
                raise CompanionStorageError("companion_state_invalid")
            for row in value[layer]:
                if not isinstance(row, dict):
                    raise CompanionStorageError("companion_state_invalid")
                for field in ("id", "content", "reason", "nature", "scope", "use_policy", "source"):
                    if field not in row:
                        raise CompanionStorageError("companion_state_invalid")
                _text(row["content"], 1024)
                _text(row["reason"], 1024, empty=True)
                if (row["scope"] not in SCOPES or row["nature"] not in ("explicit", "inferred")
                        or row["use_policy"] not in ("local_only", "cloud_allowed")
                        or not isinstance(row["source"], dict)
                        or type(row.get("created_at")) not in (int, float)
                        or not 0 <= row["created_at"] <= 1e12
                        or (row.get("expires_at") is not None and
                            (type(row["expires_at"]) not in (int, float) or not 0 <= row["expires_at"] <= 1e12))):
                    raise CompanionStorageError("companion_state_invalid")
        emotion = value["emotion"]
        if emotion is not None:
            if not isinstance(emotion, dict) or set(emotion) != {"description_intensity", "reason_sources", "time_scope", "uncertainty", "source", "expires_at", "use_policy"}:
                raise CompanionStorageError("companion_state_invalid")
            for field in ("description_intensity", "reason_sources", "time_scope", "uncertainty"):
                _text(emotion[field], 2048)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            # 仅为桌宠旧表现 DTO 提供中性投影；它不是另一份人格或情绪存储。
            return {"schema_version": 1, "revision": state["revision"], "reset_epoch": state["reset_epoch"],
                    "enabled": True, "emotions_enabled": True,
                    "persona": {"version": state["card"]["revision"], "preset": "balanced",
                                "style": {"formality": .5, "warmth": .5, "humor": .2,
                                          "verbosity": .5, "expressiveness": .4, "curiosity": .3}},
                    "persona_versions": [], "affect": {"valence": 0., "arousal": 0., "mood": 0.,
                    "mode": "neutral", "expression": "neutral"}, "warnings": [],
                    "character_card": deepcopy(state["card"])}

    def interaction_snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = self._load()
            return {"schema_version": 2, "revision": value["revision"], "reset_epoch": value["reset_epoch"],
                    "character_card": deepcopy(value["card"]), "habits": deepcopy(value["habits"]),
                    "current_emotion": deepcopy(value["emotion"]),
                    "user_understanding": deepcopy(value["user_understanding"]),
                    "interaction_approach": deepcopy(value["interaction_approach"]),
                    "learning_status": "model_feedback"}

    def context(self, conversation_id: str, conversation_epoch: int, task_id: str, *,
                allow_restricted: bool = False) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            now = self._clock()
            def active(row: dict[str, Any]) -> bool:
                if row.get("expires_at") is not None and row["expires_at"] <= now:
                    return False
                if row["scope"] == "turn":
                    return bool(row["source"].get("task_id") == task_id)
                if row["scope"] == "conversation":
                    return bool(row["source"].get("conversation_id") == conversation_id
                                and row["source"].get("conversation_epoch") == conversation_epoch)
                return True
            card = deepcopy(state["card"])
            if not allow_restricted and card["use_policy"] == "local_only":
                raise CompanionValidationError("旧角色卡仅限本地使用，请在设置中查看并重新保存后再交给当前模型。")
            def rows(layer: str) -> list[dict[str, Any]]:
                return [deepcopy(row) for row in state[layer] if active(row) and
                        (allow_restricted or row["use_policy"] == "cloud_allowed")]
            emotion = state["emotion"]
            if emotion is not None and ((emotion.get("expires_at") is not None and emotion["expires_at"] <= now)
                    or (not allow_restricted and emotion["use_policy"] == "local_only")):
                emotion = None
            if emotion is not None:
                emotion = deepcopy(emotion)
                # 时间只减弱影响，不改写事件含义，不把担忧事项当作已经解决。
                timing = emotion['source']
                since = timing.get('emotion_updated_at', 0.0)
                half_life = timing.get('emotion_half_life_seconds', 21600.0)
                if (type(since) not in (int, float) or not math.isfinite(since)
                        or type(half_life) not in (int, float) or not math.isfinite(half_life)
                        or not 900 <= half_life <= 604800):
                    raise CompanionStorageError('companion_emotion_timing_invalid')
                emotion['influence_weight'] = 2.0 ** (-min(1074.0, max(0.0, now - since) / half_life))
                expiry = emotion.get('expires_at')
                if expiry is not None and expiry > since:
                    emotion['influence_weight'] *= min(1.0, max(0.0, (expiry - now) / (expiry - since)))
                emotion['elapsed_seconds'] = max(0.0, now - since)
            result = {"role_id": card["id"], "revision": state["revision"],
                      "character_card": card, "learned_habits": rows("habits"),
                      "current_emotion": deepcopy(emotion), "user_understanding": rows("user_understanding"),
                      "interaction_approach": rows("interaction_approach"),
                      "expression_adjustment": "由模型结合上述各层及当前明确要求形成；不另存派生模式。"}
            if len(encode(result).encode("utf-8")) > 32768:
                raise CompanionValidationError("角色上下文过大，需要先整理已有要求。")
            return result

    def command(self, action: str, **parameters: Any) -> dict[str, Any]:
        allowed = {"character_card": {"text", "use_policy"}, "reset": {"target"}}
        if action not in allowed or set(parameters) - (allowed[action] | {"expected_revision", "command_id"}):
            raise CompanionValidationError("旧人格预设、短时模式及候选入口已撤除，请使用角色卡或聊天。")
        command_id = parameters.get("command_id")
        if command_id is not None:
            _text(command_id, 256)
        fingerprint = sha256(encode({"action": action, **parameters}).encode("utf-8")).hexdigest()
        with self._lock, self._repository.transaction():
            state = self._load()
            if command_id is not None:
                prior = self._repository.connection.execute("SELECT fingerprint FROM role_receipt WHERE id=?", (command_id,)).fetchone()
                if prior:
                    if prior[0] != fingerprint:
                        raise CompanionValidationError("companion_command_id_conflict")
                    return self.snapshot()
            if type(parameters.get("expected_revision")) is not int or parameters["expected_revision"] != state["revision"]:
                raise CompanionRevisionConflict("companion_revision_conflict")
            changed = True
            if action == "character_card":
                policy = parameters.get("use_policy", "cloud_allowed")
                if policy not in ("cloud_allowed", "local_only"):
                    raise CompanionValidationError("companion_use_policy_invalid")
                text = _text(parameters.get("text"), 8192)
                changed = text != state["card"]["text"] or policy != state["card"]["use_policy"]
                if changed:
                    state["card"].update(text=text, revision=state["card"]["revision"] + 1,
                                         use_policy=policy, origin="user")
            else:
                target = parameters.get("target")
                if target not in ("affect", "persona", "all"):
                    raise CompanionValidationError("companion_reset_target_invalid")
                state["emotion"] = None
                if target in ("persona", "all"):
                    replacement = initial()["card"]
                    replacement.update(id=state["card"]["id"], revision=state["card"]["revision"] + 1)
                    state["card"] = replacement
                    state["habits"] = []
                if target == "all":
                    state["user_understanding"], state["interaction_approach"] = [], []
                state["reset_epoch"] += 1
            if changed:
                state["revision"] += 1
                self._repository.save(state)
            if command_id is not None:
                self._receipt(command_id, fingerprint)
        return self.snapshot()

    def _receipt(self, identity: str, fingerprint: str) -> None:
        db = self._repository.connection
        if db.execute("SELECT COUNT(*) FROM role_receipt").fetchone()[0] >= 100000:
            raise CompanionStorageError("companion_receipt_capacity_reached")
        db.execute("INSERT INTO role_receipt VALUES(?,?)", (identity, fingerprint))

    def apply_understanding(self, task_id: str, entries: Iterable[Mapping[str, Any]], *,
                            expected_revision: int, conversation_id: str, conversation_epoch: int,
                            sequence: int, use_policy: str) -> dict[str, Any]:
        """采用一次模型理解；真实来源由宿主绑定，模型不能伪造消息编号。"""
        entries = tuple(entries)
        if len(entries) > 8 or use_policy not in ("cloud_allowed", "local_only"):
            raise CompanionValidationError("companion_understanding_invalid")
        receipt = "understand:" + _text(task_id, 200)
        fingerprint = sha256(encode(list(entries)).encode("utf-8")).hexdigest()
        with self._lock, self._repository.transaction():
            state = self._load()
            prior = self._repository.connection.execute("SELECT fingerprint FROM role_receipt WHERE id=?", (receipt,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise CompanionRevisionConflict("companion_understanding_already_processed")
                return self.interaction_snapshot()
            if state["revision"] != expected_revision:
                raise CompanionRevisionConflict("companion_revision_conflict")
            now = self._clock()
            original = encode(state)
            # 单次记录完成后不影响以后请求；过期项在下一次写入时回收。
            for layer in LAYERS:
                state[layer] = [row for row in state[layer] if row["scope"] != "turn" and
                                (row.get("expires_at") is None or row["expires_at"] > now)]
            visible = self.context(conversation_id, conversation_epoch, task_id, allow_restricted=True)
            replaceable = {row["id"] for layer in LAYERS for row in visible[layer]}
            for raw in entries:
                if (set(raw) != {"layer", "content", "reason", "nature", "scope", "expires_at", "replaces"}
                        or raw["layer"] not in LAYERS or raw["scope"] not in SCOPES
                        or raw["nature"] not in ("explicit", "inferred")
                        or not isinstance(raw["replaces"], list) or len(raw["replaces"]) > 16
                        or any(identity not in replaceable for identity in raw["replaces"])):
                    raise CompanionValidationError("companion_understanding_invalid")
                # 长期推测只能交给 P4 缓冲，不能冒充用户的明确长期约定。
                if raw["scope"] == "persistent" and raw["nature"] != "explicit":
                    raise CompanionValidationError("长期推测由反馈缓冲复核，本轮未将它作为明确长期约定保存。")
                expires = raw["expires_at"]
                if expires is not None:
                    if not isinstance(expires, str):
                        raise CompanionValidationError("companion_expiry_invalid")
                    parsed = datetime.fromisoformat(expires)
                    if parsed.tzinfo is None or not now < parsed.timestamp() <= now + 366 * 86400:
                        raise CompanionValidationError("companion_expiry_invalid")
                    expires = parsed.timestamp()
                if raw["scope"] == "day":
                    local = datetime.fromtimestamp(now, self._zone)
                    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), self._zone).timestamp()
                    expires = min(expires or midnight, midnight)
                if raw["scope"] in ("turn", "conversation", "persistent") and raw["expires_at"] is None:
                    expires = None
                layer = raw["layer"]
                for key in LAYERS:
                    state[key] = [row for row in state[key] if row["id"] not in raw["replaces"]]
                content = _text(raw["content"], 1024, empty=True)
                if content:
                    state[layer].append({"id": uuid4().hex, "content": content,
                        "reason": _text(raw["reason"], 1024, empty=True), "nature": raw["nature"],
                        "scope": raw["scope"], "created_at": now, "expires_at": expires,
                        "use_policy": use_policy, "source": {"kind": "dialogue_turn", "task_id": task_id,
                            "id": task_id + ":user", "revision": sequence, "conversation_id": conversation_id,
                            "conversation_epoch": conversation_epoch}})
            self._validate(state)
            if encode(state) != original:
                state["revision"] += 1
            self._repository.save(state)
            self._receipt(receipt, fingerprint)
        return self.interaction_snapshot()

    def clear_conversation(self, conversation_id: str) -> None:
        with self._lock, self._repository.transaction():
            state = self._load()
            changed = False
            for key in LAYERS:
                kept = [row for row in state[key] if not (row["scope"] in ("turn", "conversation") and
                        row["source"].get("conversation_id") == conversation_id)]
                changed = changed or kept != state[key]
                state[key] = kept
            records = [*state['habits'], *state['user_understanding'], *state['interaction_approach']]
            if state['emotion']:
                records.append(state['emotion'])
            for record in records:
                for ref in record['source'].get('refs', []):
                    if ref.get('kind') == 'dialogue_turn' and ref.get('conversation_id') == conversation_id and ref.get('available', True):
                        ref['available'] = False
                        changed = True
            if changed:
                state["revision"] += 1
                self._repository.save(state)


    def close_conversation(self, conversation_id: str) -> None:
        # 关闭窗口不等于清空会话，跨轮有效要求随持久会话保留。
        return None

    def apply_learning(self, command_id: str, *, expected_revision: int,
                       source: Mapping[str, Any], habits: list[dict[str, Any]],
                       emotion: dict[str, Any] | None, use_policy: str) -> int:
        """跨库投递的幂等接收端；回执、局部成长和情绪在角色库一次提交。"""
        fingerprint = sha256(encode(dict(source=source, habits=habits, emotion=emotion,
            use_policy=use_policy, expected_revision=expected_revision)).encode()).hexdigest()
        with self._lock, self._repository.transaction():
            state = self._load()
            prior = self._repository.connection.execute('SELECT fingerprint FROM role_receipt WHERE id=?', (command_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise CompanionValidationError('companion_command_id_conflict')
                return int(state['revision'])
            if state['revision'] != expected_revision:
                raise CompanionRevisionConflict('companion_revision_conflict')
            if use_policy not in ('cloud_allowed', 'local_only') or not source.get('task_id'):
                raise CompanionValidationError('companion_feedback_source_invalid')
            for habit in habits:
                identity = habit['id']
                old = next((row for row in state['habits'] if row['id'] == identity), None)
                if identity is not None and old is None:
                    raise CompanionRevisionConflict('Habit no longer exists')
                if old is not None:
                    state['habits'].remove(old)
                if habit['content']:
                    state['habits'].append(dict(id=identity or uuid4().hex,
                        content=_text(habit['content'], 1024), reason=_text(habit['reason'], 1024),
                        nature='explicit' if habit['explicit'] else 'inferred', scope='persistent',
                        created_at=self._clock(), expires_at=None, use_policy=use_policy,
                        uncertainty=habit['uncertainty'], source=dict(source, refs=habit['host_refs'])))
            if emotion is not None:
                expiry = emotion['expires_at']
                if expiry is not None and not self._clock() < expiry <= 1e12:
                    raise CompanionValidationError('companion_expiry_invalid')
                state['emotion'] = {key: _text(emotion[key], 2048) for key in
                    ('description_intensity', 'reason_sources', 'time_scope', 'uncertainty')}
                half_life = emotion.get('half_life_seconds', 21600.0)
                if (type(half_life) not in (int, float) or not math.isfinite(half_life)
                        or not 900 <= half_life <= 604800):
                    raise CompanionValidationError('companion_emotion_timing_invalid')
                learned_at = source.get('learning_at', self._clock())
                if type(learned_at) not in (int, float) or not math.isfinite(learned_at) or not 0 <= learned_at <= 1e12:
                    raise CompanionValidationError('companion_emotion_timing_invalid')
                state['emotion'].update(source=dict(source, refs=emotion['host_refs'],
                    emotion_updated_at=min(learned_at, self._clock()), emotion_half_life_seconds=half_life),
                    expires_at=expiry, use_policy=use_policy)
            self._validate(state)
            state['revision'] += 1
            self._repository.save(state)
            # 自动成长不能保存成下一轮连必需角色资料都无法读取的状态。
            self.context(str(source.get('conversation_id', 'default')), int(source.get('conversation_epoch', 0)),
                         str(source['task_id']), allow_restricted=True)
            self._receipt(command_id, fingerprint)
            return int(state['revision'])

    def style_instruction(self, request: str, *, cloud: bool = True,
                          preferences: dict[str, Any] | None = None,
                          constraints: dict[str, Any] | None = None, mode: str | None = None,
                          conversation_id: str = "default", use_policy: str = "cloud_allowed") -> str:
        return encode(self.context(conversation_id, 0, "", allow_restricted=not cloud))

    def reset_for_privacy(self) -> None:
        with self._lock, self._repository.transaction():
            state = self._load()
            state["emotion"] = None
            state["user_understanding"], state["interaction_approach"] = [], []
            state["reset_epoch"] += 1
            state["revision"] += 1
            self._repository.save(state)

    def invalidate_interaction(self) -> None:
        # 失效由持久来源/修订落实；关闭后台不能删除仍有效的会话要求。
        return None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._repository.close()
