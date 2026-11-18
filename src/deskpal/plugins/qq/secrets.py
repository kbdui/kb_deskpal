"""凭据只放系统凭据库或环境变量，不放聊天库、备份或页面回显。"""
from hashlib import sha256
import os
from pathlib import Path
import re

from deskpal.credentials import WindowsCredentialStore, CredentialStoreError


class PluginVault(WindowsCredentialStore):
    @staticmethod
    def valid_ref(reference: str) -> bool:
        return isinstance(reference, str) and re.fullmatch(r'Deskpal/Plugin/qq/[0-9a-f]{32}/(token|event_secret)', reference) is not None


class QqSecrets:
    def __init__(self, profile: Path) -> None:
        self.identity = sha256(str(profile.resolve()).encode()).hexdigest()[:32]
        self.vault = PluginVault()

    def get(self, key: str) -> str:
        value = os.environ.get('DESKPAL_QQ_' + key.upper())
        if value is not None:
            return value
        if not self.vault.available():
            return ''
        return self.vault.read('Deskpal/Plugin/qq/' + self.identity + '/' + key) or ''

    def put(self, key: str, value: str) -> None:
        if key not in {'token', 'event_secret'} or not value:
            raise ValueError('QQ 凭据类型或内容无效')
        external = os.environ.get('DESKPAL_QQ_' + key.upper())
        if external is not None:
            if external != value:
                raise ValueError('已配置 QQ 凭据环境变量，界面不能覆盖；请更新对应环境变量后重启。')
            return
        if not self.vault.available():
            raise CredentialStoreError()
        self.vault.write('Deskpal/Plugin/qq/' + self.identity + '/' + key, value)

    def erase(self) -> None:
        if self.vault.available():
            for key in ('token', 'event_secret'):
                self.vault.delete('Deskpal/Plugin/qq/' + self.identity + '/' + key)
