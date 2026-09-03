"""
Pack simulation - calibrated to actual server (top player OVR 86, base 7M)
"""
import math, random

def base_value(age, ovr, pot):
    core = int(22_000 * (1.20 ** max(0, ovr - 55)))
    gap  = max(0, pot - ovr)
    pm   = 1.0 + gap * (0.030 if age<=21 else 0.018 if age<=26 else 0.008)
    af   = max(0.20, 1.0-(age-29)*0.12) if age>=30 else 1.0
    return max(10_000, int(core * pm * af))

def grade(pot):
    if pot>=90: return "S"
    if pot>=84: return "A"
    if pot>=78: return "B"
    if pot>=72: return "C"
    return "D"

def spawn():
    """
    실제 서버 모사:
    - 일부는 신생(갓 생성), 일부는 성장한 선수
    - 상위권은 OVR 80대 선수들 존재
    """
    age = random.randint(17, 28)
    r = random.random()
    if   r < 0.08: pot = random.randint(90, 95)
    elif r < 0.22: pot = random.randint(84, 89)
    elif r < 0.55: pot = random.randint(78, 83)
    elif r < 0.85: pot = random.randint(72, 77)
    else:          pot = random.randint(66, 71)

    # 나이가 많을수록 ovr이 pot에 가까움 (성장 시뮬)
    if age <= 20:
        gap = random.randint(12, 30)
    elif age <= 23:
        gap = random.randint(6, 20)
    elif age <= 26:
        gap = random.randint(2, 12)
    else:
        gap = random.randint(0, 6)

    ovr = max(50, pot - gap)
    bv  = base_value(age, ovr, pot)
    return {"ovr": ovr, "pot": pot, "age": age, "bv": bv, "grade": grade(pot)}

def w(pp, pk):
    p, P = max(1.0, float(pp)), max(1.0, float(pk))
    if p <= P:
        return math.exp(-0.5*((P-p)/(P*0.35))**2)
    return 0.80*math.exp(-0.5*((p-P)/(P*0.55))**2)

def fmt(n):
    if n>=1_000_000: return f"{n/1_000_000:.2f}M"
    if n>=1_000:     return f"{n/1_000:.0f}k"
    return str(n)

def simulate(packs, pool, n=60_000):
    sorted_pool = sorted(pool, key=lambda x: x["bv"], reverse=True)
    print(f"\n  {'Pack':<10} {'Price':>8}  {'AvgVal':>9}  {'EV':>5}  {'S%':>4}  {'A%':>4}  {'B%':>4}  {'Min':>8}  {'Max':>8}")
    print(f"  {'-'*10} {'-'*8}  {'-'*9}  {'-'*5}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*8}  {'-'*8}")
    for name, p in packs.items():
        cands = sorted_pool[p["rf"]-1 : min(p["rt"], len(sorted_pool))]
        if not cands: continue
        pk = p["price"]
        ws = [w(c["bv"], pk) for c in cands]
        bvs, gc = [], {"S":0,"A":0,"B":0,"C":0,"D":0}
        for _ in range(n):
            pick = random.choices(cands, weights=ws, k=1)[0]
            bvs.append(pick["bv"]); gc[pick["grade"]] += 1
        avg = int(sum(bvs)/len(bvs))
        print(f"  {name:<10} {fmt(pk):>8}  {fmt(avg):>9}  {avg/pk:>5.2f}x"
              f"  {gc['S']/n*100:>3.0f}%  {gc['A']/n*100:>3.0f}%  {gc['B']/n*100:>3.0f}%"
              f"  {fmt(min(bvs)):>8}  {fmt(max(bvs)):>8}")

if __name__ == "__main__":
    random.seed(7)
    POOL = [spawn() for _ in range(400)]
    s = sorted(POOL, key=lambda x: x["bv"], reverse=True)

    # 실제 서버 top 선수를 OVR 86 / 7M 으로 스케일 맞춤 (seed 조정)
    print("=== 실제 서버 추정 선수 분포 (400명) ===")
    for rank in [1, 5, 10, 20, 30, 60, 100, 200, 300]:
        if rank <= len(s):
            p = s[rank-1]
            print(f"  {rank:>3}위: OVR {p['ovr']:>2}  {p['grade']}급  기준가 {fmt(p['bv'])}")

    # ── 팩 세트 비교 ──────────────────────────────────────
    sets = {
        "A. 현재":   {"Bronze":{"price":   45_000,"rf":101,"rt":300},
                      "Silver": {"price":   60_000,"rf": 61,"rt":200},
                      "Gold":   {"price":  135_000,"rf": 31,"rt":120},
                      "Plat":   {"price":  300_000,"rf": 11,"rt": 60},
                      "Icon":   {"price":  550_000,"rf":  1,"rt": 30}},
        "B. 제안1":  {"Bronze":{"price":  150_000,"rf":101,"rt":300},
                      "Silver": {"price":  350_000,"rf": 61,"rt":200},
                      "Gold":   {"price":  900_000,"rf": 31,"rt":120},
                      "Plat":   {"price":2_500_000,"rf": 11,"rt": 60},
                      "Icon":   {"price":6_000_000,"rf":  1,"rt": 30}},
        "C. 제안2":  {"Bronze":{"price":  100_000,"rf":101,"rt":300},
                      "Silver": {"price":  200_000,"rf": 61,"rt":200},
                      "Gold":   {"price":  500_000,"rf": 31,"rt":120},
                      "Plat":   {"price":1_200_000,"rf": 11,"rt": 60},
                      "Icon":   {"price":3_000_000,"rf":  1,"rt": 30}},
    }

    for label, packs in sets.items():
        print(f"\n{'='*70}")
        print(f"  {label}")
        print(f"{'='*70}")
        simulate(packs, POOL)

    # ── 아이콘팩 딱 맞는 가격 찾기 ────────────────────────
    print("\n\n=== 아이콘팩 EV 1.2x 달성하는 적정 가격 탐색 ===")
    icon_cands = s[0:30]
    avg_real = int(sum(p["bv"] for p in icon_cands) / len(icon_cands))
    print(f"  상위 30명 단순 평균 기준가: {fmt(avg_real)}")
    print(f"  EV 1.0x 목표 팩 가격: {fmt(avg_real)}")
    print(f"  EV 1.2x 목표 팩 가격: {fmt(int(avg_real/1.2))}")
    print(f"  EV 1.5x 목표 팩 가격: {fmt(int(avg_real/1.5))}")
