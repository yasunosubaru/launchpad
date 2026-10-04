# -*- coding: utf-8 -*-
"""
删除确认对话框。

单独一个模块而不是塞进 window.py：这个对话框有它自己的信息职责 ——
它必须把「删掉的是启动器里的条目，不是用户的快捷方式」讲清楚，否则
用户会以为要点两次才能删干净，或者怕误删而不敢点。

## 为什么不用 QMessageBox.question

三个理由，都是实测/源码层面的：

1. **它是模态的**（``exec_()`` 起嵌套事件循环）。这本身没问题，
   但 QMessageBox 的按钮会被 QWindowsVistaStyle 画成浅灰底蓝字的
   系统外观，和整个纯黑深色界面割裂 —— 和菜单同一个问题，
   菜单已经自己写了 QSS 解决，对话框也得跟上。
2. **默认按钮是「确定」还是「取消」不可控**，而且危险操作用默认按钮
   是坏习惯。这里把「取消」设为默认与 Esc，取消动作必须是最容易按到的。
3. 提示文案需要分段（一段问句、一段后果说明），QMessageBox 的
   informativeText 缩进样式在深色下很难看。

## 文案里必须说清楚的两件事

* 删的是**启动器里的条目**，磁盘上的源文件不动 —— 这决定了用户敢不敢点，
  也决定了「删掉了为什么还能在文件夹里看到它」不会变成 bug 报告。
* 重新导入文件夹时它不会回来（墓碑机制，见 ``library.Library.remove``）。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QVBoxLayout)


#: 配色沿用 blankarea.MENU_QSS 与 theme.py：#14161c 底 / #eef0f4 字
#: / #2a2e38 描边 / #2b6cb0 强调。
_DIALOG_QSS = """
QDialog{ background:#14161c; }
QLabel{ color:#eef0f4; font-size:13px; }
QLabel#name{ color:#ffffff; font-size:15px; font-weight:600; }
QLabel#note{ color:#9aa4b2; font-size:12px; }
QPushButton{ background:#22262f; color:#eef0f4; border:1px solid #2a2e38;
             border-radius:7px; padding:7px 20px; font-size:13px; }
QPushButton:hover{ background:#2b3038; }
QPushButton#danger{ background:#8f2f2f; border-color:#a83a3a; color:#fff; }
QPushButton#danger:hover{ background:#a83636; }
"""


class DeleteDialog(QDialog):
    """
    「从启动器移除 <名字>」的确认框。

    用法与其它对话框一致::

        dlg = DeleteDialog(entry, parent)
        if dlg.exec_() == DeleteDialog.Accepted:
            ...

    ``Accepted`` 只表示「用户点了移除」，**不含**真正写盘 ——
    写盘由调用方在拿到 Accepted 之后做，失败还能给用户报错。
    """

    def __init__(self, entry, parent=None, extra_note: str = ""):
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle("从启动器移除")
        self.setModal(True)
        self.setStyleSheet(_DIALOG_QSS)
        # 自绘标题栏风格的窗口在 Windows 上默认最小化/最大化按钮用不了，
        # 关掉最大化；setFixedWidth 是为了让按钮换行时不至于把布局撑歪。
        self.setMaximumWidth(460)

        name = getattr(entry, "name", "") or "(未命名)"

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(10)

        title = QLabel("从启动器移除这个应用？")
        title.setObjectName("name")
        root.addWidget(title)

        body = QLabel(name)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(body)

        note = QLabel(
            "只从启动器移除。磁盘上的源文件不会被删除，"
            "以后重新导入文件夹也不会再出现。")
        note.setObjectName("note")
        note.setWordWrap(True)
        root.addWidget(note)

        if extra_note:
            more = QLabel(extra_note)
            more.setObjectName("note")
            more.setWordWrap(True)
            root.addWidget(more)

        root.addSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(10)
        # 顶一个弹簧把按钮推到右边。
        spring = QLabel()
        spring.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(spring, 1)

        cancel = QPushButton("取消")
        cancel.setObjectName("cancel")
        cancel.clicked.connect(self.reject)
        # 取消是默认按钮：危险操作里，最容易按到的那个绝不能是执行删除的。
        #
        # **用 ``QPushButton.setDefault(True)``，不是 ``QDialog.setDefaultWidget``。**
        # 后者根本不存在 —— 那是 ``QMessageBox`` 的 API。这里原来写的是
        # ``self.setDefaultWidget(cancel)``，于是对话框**一构造就抛
        # AttributeError**，「从启动器移除」一点就崩。
        #
        # 为什么一直没被发现：``tests_remove`` 把 ``RD.DeleteDialog`` 换成了
        # 假对话框来驱动 ``_delete_entry``，真对话框的 ``__init__`` 从未被
        # 执行过。**测试替身越顺手，被替身盖住的那段就越容易腐烂。**
        cancel.setDefault(True)
        cancel.setAutoDefault(True)

        remove = QPushButton("移除")
        remove.setObjectName("danger")
        remove.clicked.connect(self.accept)
        # 刻意不设 autoDefault：Tab 过去不会误触发移除。
        remove.setAutoDefault(False)

        row.addWidget(cancel)
        row.addWidget(remove)
        root.addLayout(row)


# 供测试与外部按名字取（PEP 562 的惰性导出让这里没必要，直接赋值即可）。
DeleteDialog.Accepted = QDialog.Accepted
DeleteDialog.Rejected = QDialog.Rejected