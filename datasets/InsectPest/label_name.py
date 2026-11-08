# -*- coding: utf-8 -*-
"""
@Auth ： 思绪无限
博客园、知乎：思绪无限
Bilibili：思绪亦无限
公众号：AI技术研究与分享
代码地址见以下博客中给出:
https://www.cnblogs.com/sixuwuxian/
https://www.zhihu.com/people/sixuwuxian

@IDE ：PyCharm
运行本项目需要python3.8及以下依赖库（完整库见requirements.txt）：
    opencv-python==4.5.5.64
    tensorflow==2.9.1
    PyQt5==5.15.6
    scikit-image==0.19.3
    torch==1.8.0
    keras==2.9.0
    Pillow==9.0.1
    scipy==1.8.0
点击运行主程序runMain.py，程序所在文件夹路径中请勿出现中文
"""
Chinese_name = {"Hellula undalis": "小菜蛾",
                "Leaf Webber": "叶网蛾",
                "ash weevil": "灰象甲",
                "blister beetle": "水泡甲虫",
                "fruit fly": "果蝇",
                "fruit sucking moth": "吸果蛾",
                "helicoverpa": "棉铃虫",
                "leucinodes": "茄果螟",
                "mealy bug": "粉虱",
                "pieris": "菜粉蝶",
                "plutella": "小菜蛾",
                "root grubs": "根蛆",
                "schizaphis graminum": "禾谷缢管蚜",
                "uroleucon compositae": "合头菊蚜",
                "whitefly": "白粉虱"}
Label_list = list(Chinese_name.values())

