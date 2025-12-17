import os
import time
import datetime
import sys

# Path to EVE logs
LOG_DIR = os.path.expanduser("~/Documents/EVE/logs/Chatlogs")
FILENAME = f"Fleet_SIMULATED_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
FILEPATH = os.path.join(LOG_DIR, FILENAME)

def write_line(f, sender, message):
    # EVE format: [ 2025.12.16 08:38:43 ] Sender > Message
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y.%m.%d %H:%M:%S")
    line = f"[ {timestamp} ] {sender} > {message}\n"
    f.write(line)
    f.flush()
    print(f"Wrote: {line.strip()}")

def main():
    print(f"Creating simulated log: {FILEPATH}")

    # Open with UTF-16LE
    with open(FILEPATH, 'w', encoding='utf-16-le') as f:
        # Header
        f.write("\ufeff") # BOM
        f.write("--------------------------------------------------------------------\n")
        f.write("  Channel ID:      fleet_12345\n")
        f.write("  Channel Name:    Fleet\n")
        f.write("  Listener:        SimulatedUser\n")
        f.write("--------------------------------------------------------------------\n\n")

        print("File created. Waiting 5s for listener...")
        time.sleep(5)

        # Real-world fleet conversation (20 messages max, mixed languages + EVE slang)
        write_line(f, "Dragon_FC", "舰队准备出发，大家锚定我") # Fleet ready to go, everyone anchor on me
        time.sleep(3)

        write_line(f, "Wushi_Logi", "后勤准备好了") # Logi ready
        time.sleep(3)

        write_line(f, "BearHunter", "我的毒蜥准备好了") # My Gila is ready
        time.sleep(3)

        write_line(f, "IvanRUS", "Готов, давайте") # Russian: Ready, let's go
        time.sleep(3)

        write_line(f, "Dragon_FC", "所有人对齐星门，不要过门") # Everyone align to gate, don't jump
        time.sleep(3)

        write_line(f, "NewbieOne", "What gate?") # English (should be ignored by default)
        time.sleep(3)

        write_line(f, "Hans_DE", "Ausgerichtet") # German: Aligned
        time.sleep(3)

        write_line(f, "Dragon_FC", "跳！走星门！") # Jump! Take the gate!
        time.sleep(3)

        write_line(f, "Scout_Alex", "敌人舰队！10个响尾蛇，5个复仇者！") # Enemy fleet! 10 Rattlesnakes, 5 Vindicators!
        time.sleep(3)

        write_line(f, "Dragon_FC", "集火 \x1ARattlesnake Alpha\x1A 锁定开火！") # Primary Rattlesnake Alpha, lock and fire!
        time.sleep(3)

        write_line(f, "BearHunter", "反跳上了，网子上了！") # Scram and web applied!
        time.sleep(3)

        write_line(f, "Wushi_Logi", "摇修给 \x1ABearHunter\x1A") # Repping BearHunter
        time.sleep(3)

        write_line(f, "IvanRUS", "У меня нет капы!") # Russian: I have no cap!
        time.sleep(3)

        write_line(f, "Caplogi_Zhang", "给电！注油给 \x1AIvanRUS\x1A") # Cap transfer! Cap to IvanRUS
        time.sleep(3)

        write_line(f, "Dragon_FC", "转火 \x1AVindicator Beta\x1A 超载武器！") # Switch to Vindicator Beta, overheat!
        time.sleep(3)

        write_line(f, "Scout_Alex", "敌人援军进站了") # Enemy reinforcements docked
        time.sleep(3)

        write_line(f, "Dragon_FC", "打得不错！现在撤退，对齐终点") # Good fight! Now retreat, align to destination
        time.sleep(3)

        write_line(f, "Hans_DE", "GF o7") # Good fight salute (English)
        time.sleep(3)

        write_line(f, "BearHunter", "666 牛逼！") # 666 awesome! (Chinese slang)
        time.sleep(3)

        write_line(f, "Dragon_FC", "所有人跃迁，回家了！") # Everyone warp, going home!

        print("Simulation done - 20 messages with mixed languages & EVE slang.")
        time.sleep(2)

if __name__ == "__main__":
    main()
