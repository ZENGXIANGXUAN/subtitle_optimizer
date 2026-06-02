import os

path = r'D:\xwechat_files\wxid_yval4poj1ox422_ec06\msg\file\2026-01\MIKE合集_字幕\Valtos OrderFlows\Valtos OrderFlows字幕'
def readandreplae(filename):
    with open(filename, 'r',encoding='utf8') as f:
        file_content = f.read()
    L = file_content.split('\n\n')
    newL = []
    for l in L:
        _ = l.split('\n')

        if len(_)==4:
            del _[2]
        newL.append("\n".join(_))
        # print("*"*30)
    with open(filename, 'w',encoding='utf8') as f:
        f.write("\n\n".join(newL))
for root, dirs, files in os.walk(path):
    for file in files:
        if file.endswith(".srt"):
            print(os.path.join(root, file))
            readandreplae(os.path.join(root, file))
            # exit()