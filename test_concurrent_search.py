import asyncio
from owid_tools import Tools

async def main():
    tools = Tools()
    
    # Test single word
    res = await tools.search_owid("life")
    print("Results for 'life':")
    for line in res.split('\n')[:5]:
        print(line)
    
    print("-" * 50)
    
    # Test multiple words
    res = await tools.search_owid("life expectancy CO2")
    print("Results for 'life expectancy CO2':")
    # Just print the slug lines
    slugs = [line for line in res.split('\n') if 'slug:' in line]
    print(f"Total slugs found: {len(slugs)}")
    for s in slugs[:10]:
        print(s)

asyncio.run(main())
