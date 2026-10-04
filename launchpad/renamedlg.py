# -*- coding: utf-8 -*-
"""
「重命名」对话框 —— 给启动器里的条目起一个自定义显示名。

单独一个模块、样式复用 removedlg 的深色主题：两个对话框是同一类东西
（都是「改一下启动器里的这个条目」的二次确认 + 输入），分裂成两套外观
只会让界面看起来是拼的。

## 语义：加一层显示名，不是覆盖原名

**磁盘上的 ``.lnk`` 一个字节都不动**，``Entry.name`` 保持从 .lnk 读出来的
原值，用户起的名字进 ``renames.json``（uid -> 显示名），由
``Entry.label`` 决定界面显示什么。

这么设计有三个理由，每一个都对应一个具体的坑：

1. **原名必须还能搜到。** 用户在开始菜单里看到的是「微信」，他多半就拿
   「微信」来搜。只搜自定义名的话，改完名等于把这个应用从搜索里弄丢 ——
   而磁盘上的快捷方式根本没人动过，凭什么它的名字就消失了。
   （``Library.search`` 同时匹配 ``name`` 与 ``display``。）

2. **重新导入会丢。** 库为空时的首次自动导入、「添加应用 → 浏览文件夹」
   都会从 .lnk 重建 Entry，``name`` 被刷回原值。和墓碑要解决的是同一个
   问题，所以用同一种解法：磁盘上存一份uid 映射，每次重建后重新贴回。

3. **uid 会跟着变。** 读不出 target 的条目 uid 是
   ``sha1(source_lnk|name)``，直接改 name 就换了 uid —— 墓碑、去重、
   图标缓存键全部对不上。

## 为什么不用 QInputDialog

和 removedlg 同样的理由：系统默认外观（浅灰底蓝字）与整个纯黑深色界面
割裂，而且我们需要「原名是什么」「清空会还原」这些说明文字，
QInputDialog 只有一个输入框加两个按钮，塞不下。

## 空输入 = 还原原名

这是刻意的：一个输入框同时承担「改名」和「撤销改名」，不需要第二个按钮
或第二个对话框。清空输入框就回到 .lnk 里的原名。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QSizePolicy, QVBoxLayout)

#: 配色与 removedlg._DIALOG_QSS 保持一致（同一类界面，两套外观会显得是拼的）。
_DIALOG_QSS = """
QDialog{ background:#14161c; }
QLabel{ color:#eef0f4; font-size:13px; }
QLabel#title{ color:#ffffff; font-size:15px; font-weight:600; }
QLabel#note{ color:#9aa4b2; font-size:12px; }
QLineEdit{ background:#0f1117; color:#eef0f4; border:1px solid #2a2e38;
           border-radius:7px; padding:8px 10px; font-size:14px;
           selection-background-color:#2b6cb0; }
QLineEdit:focus{ border-color:#2b6cb0; }
QPushButton{ background:#22262f; color:#eef0f4; border:1px solid #2a2e38;
             border-radius:7px; padding:7px 20px; font-size:13px; }
QPushButton:hover{ background:#2b3038; }
QPushButton#primary{ background:#2b6cb0; border-color:#3b7fd0; color:#fff; }
QPushButton#primary:hover{ background:#3379c4; }
QPushButton#primary:disabled{ background:#23262e; border-color:#2a2e38;
                             color:#5c6472; }
"""


class RenameDialog(QDialog):
    """
    「重命名」输入框。``Accepted`` 时用 :meth:`value` 取新名字。

    :meth:`value` 返回**已 strip 的字符串**；空串表示「还原原名」。

        dlg = RenameDialog(entry, parent)
        if dlg.exec_() == RenameDialog.Accepted:
            library.rename(entry.uid, dlg.value())
    """

    #: 输入框的最大长度。够长（很多 .lnk 的名字本来就上百字），但不是无限 ——
    #: 用户粘进来一整段话时按钮还是该能点。
    MAX_LEN = 120

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle("重命名")
        self.setModal(True)
        self.setStyleSheet(_DIALOG_QSS)
        self.setMaximumWidth(480)

        orig = getattr(entry, "name", "") or "(未命名)"
        cur = getattr(entry, "label", "") or orig
        was_renamed = bool(getattr(entry, "renamed", False))

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(10)

        title = QLabel("给这个应用起个名字")
        title.setObjectName("title")
        root.addWidget(title)

        info = QLabel(f"原名：{orig}")
        info.setObjectName("note")
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(info)

        self.edit = QLineEdit(cur)
        self.edit.setMaxLength(self.MAX_LEN)
        self.edit.selectAll()          # 直接打字覆盖，最常见的一步
        self.edit.setPlaceholderText(orig)
        root.addWidget(self.edit)

        note = QLabel(
            "只改启动器里的显示名，磁盘上的源快捷方式不动，"
            "原名也仍然搜得到。")
        if was_renamed:
            note.setText(note.text() + "清空输入框可以还原原名。")
        else:
            note.setText(note.text() + "留空或填原名 = 不改名。")
        note.setObjectName("note")
        note.setWordWrap(True)
        root.addWidget(note)

        root.addSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(10)
        spring = QLabel()
        spring.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(spring, 1)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        # **回车 = 保存**，不是取消。
        #
        # 输入框里的回车是用户最顺手的提交动作，而 autoDefault 的语义是
        # 「焦点在输入框时按回车也走这个默认按钮」—— 所以取消按钮必须
        # autoDefault=False，否则每敲一次名字就取消了。
        #
        # 默认按钮用 ``QPushButton.setDefault(True)``：``QDialog`` 没有
        # ``setDefaultWidget``（那是 QMessageBox 的 API），removedlg.py
        # 里就踩过这个坑 —— 一构造就 AttributeError。
        self.cancel_btn.setAutoDefault(False)

        self.ok_btn = QPushButton("保存")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.clicked.connect(self.accept)
        self.ok_btn.setDefault(True)
        self.ok_btn.setAutoDefault(False)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.ok_btn)
        root.addLayout(row)

        # 「改成和原名一样」不算改动 —— 这时禁用保存，免得用户以为自己
        # 改了名、结果什么都没发生（下次打开又看到原名，以为功能坏了）。
        self.edit.textChanged.connect(self._sync_ok)
        self._sync_ok(self.edit.text())
        self.edit.setFocus()

    def _sync_ok(self, text: str) -> None:
        """
        相对当前显示名没有实际改动时禁用「保存」。

        否则用户敲回原来的名字、点保存、看什么都没发生 —— 下次打开又看到
        原名，只能得出「改名功能坏了」。

        「清空输入框」在当前已是自定义名时**是**有效操作（= 还原原名），
        所以它要能通过这个判断。
        """
        cur = (text or "").strip()
        cur_label = (getattr(self.entry, "label", "") or "").strip()
        if not cur:
            # 只有「本来就有自定义名」时，清空才是还原操作
            self.ok_btn.setEnabled(bool(cur_label))
            return
        self.ok_btn.setEnabled(cur != cur_label)

    def value(self) -> str:
        """新显示名，已 strip。空串 = 还原原名。"""
        return (self.edit.text() or "").strip()

    def changed(self) -> bool:
        """相对当前状态是否真的有改动（用来决定要不要禁用保存）。"""
        return self.value() != ((getattr(self.entry, "label", "") or "").strip())


RenameDialog.Accepted = QDialog.Accepted
RenameDialog.Rejected = QDialog.Rejected