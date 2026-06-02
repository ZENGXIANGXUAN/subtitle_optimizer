import os
import shutil
path = r'G:\我的云端硬盘\订单流\John\Strategic Trading - Market Profile Trading Strategies - Beyond the Basics'
for root, dirs, files in os.walk(path):
    for file in files:
        if file.endswith(".srt"):
            newfile = os.path.join(root, file).replace("srt", "txt")
            # print(newfile)
            shutil.copy(os.path.join(root, file), os.path.join(root, newfile))
