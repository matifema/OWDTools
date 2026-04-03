import asyncio
from owid_tools import Tools


async def run_tests():
    t = Tools()

    print("=== Testing search_owid ===")
    res1 = await t.search_owid("CO2 emissions")
    print(res1[:300] + "...\n")

    print("=== Testing chart_owid_data ===")
    res2 = await t.chart_owid_data("life-expectancy", "Italy")
    # res2 should be an HTMLResponse object or string (if HTMLResponse is not present, though we installed fastapi)
    if hasattr(res2, "body"):
        print(f"Success! Returned HTMLResponse. Body length: {len(res2.body)} bytes\n")
    else:
        print(f"Returned string. Length: {len(res2)}\n")

    print("=== Testing compare_owid_countries ===")
    res3 = await t.compare_owid_countries(
        "life-expectancy", ["Italy", "France", "Germany"]
    )
    if hasattr(res3, "body"):
        print(f"Success! Returned HTMLResponse. Body length: {len(res3.body)} bytes\n")
    else:
        print(f"Returned string. Length: {len(res3)}\n")

    print("=== Testing get_owid_data ===")
    res4 = await t.get_owid_data("life-expectancy", "Italy")
    print(res4[:400] + "...\n")


if __name__ == "__main__":
    asyncio.run(run_tests())
