import os
import shutil

path = r'G:\我的云端硬盘\订单流\axia\Axia Futures - The Footprint Edge'
for root, dirs, files in os.walk(path):
    for file in files:
        if file.endswith(".srt") and "中配" not in root:
            # print(os.path.join(root, file))
            newpath = os.path.join(root, "中配")
            # print(newpath)
            # print(os.path.join(root, file))
            # f = open(os.path.join(root, file), "r")
            # f.close()
            try:
                shutil.copy(os.path.join(root, file), os.path.join(newpath, file)) #
            except Exception as e:
                print(newpath)
                print(e)
                continue