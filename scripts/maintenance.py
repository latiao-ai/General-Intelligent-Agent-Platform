"""Run the configured retention policy once (also scheduled by the API)."""
import asyncio
from webpage_agent.store.persistence import open_store
from webpage_agent.observe import maintenance


async def main():
    store = open_store()
    try:
        await store.ready()
        print(await maintenance.run_once(store))
    finally:
        await store.close()


if __name__ == '__main__':
    asyncio.run(main())
