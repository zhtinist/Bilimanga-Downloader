"""bilimanga.net 漫画 / 哔哩轻小说下载器。

插件化架构：内容源（sources）→ 打包器（packagers）→ 存储去向（storage），由
registry 注册、app 编排、ui 交互。加新站点/新格式/新云盘只需新增一个插件文件。
"""

__version__ = "3.0.0"
