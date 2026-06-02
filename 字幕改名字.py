import os

path = r'G:\我的云端硬盘\订单流\Jim Dalton\Market Profile Advanced Nuances & Exceptions - Jim Dalton (2020)'
for root, dirs, files in os.walk(path):
    for file in files:
        if file.endswith(".srt"):
            new_filename = file.replace("【字幕】", "")
            new_filename = new_filename.replace("-LLM 大模型翻译", "")
            new_filename = new_filename.replace("_optimized", "")

            if new_filename == file:
                continue

            old_path = os.path.join(root, file)
            new_path = os.path.join(root, new_filename)

            # os.replace 在目标文件存在时会直接覆盖
            os.replace(old_path, new_path)
            print(f"🔄 已重命名(如有冲突已覆盖): {file} -> {new_filename}")