"""Read-only Streamable HTTP MCP check; run using the mcp environment.

uv run --directory mcp python ../scripts/mcp_smoke.py --project-id PROJECT
Optional --download-dir writes an existing selected walk PNG on THIS client.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(args):
    async with streamablehttp_client(args.url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = {tool.name for tool in (await session.list_tools()).tools}
            required = {'get_generation_config', 'get_project', 'list_takes', 'inspect_candidate_frames', 'get_candidate_download'}
            if missing := required - available:
                raise RuntimeError(f'Missing tools: {sorted(missing)}')

            async def call(name, **arguments):
                result = await session.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError(f'{name}: {result.content}')
                return json.loads(next(block.text for block in result.content if block.type == 'text'))

            config = await call('get_generation_config')
            report = {'tools': len(available), 'config': config, 'mutations': 0}
            if args.project_id:
                await call('get_project', project_id=args.project_id)
                takes = await call('list_takes', project_id=args.project_id, motion_id='walk')
                selected = takes['selected_candidate_id']
                if not selected:
                    raise RuntimeError('Project has no selected walk')
                frames = await session.call_tool('inspect_candidate_frames', dict(project_id=args.project_id, motion_id='walk', candidate_id=selected, start=0, limit=1))
                if frames.isError or not any(b.type == 'image' for b in frames.content):
                    raise RuntimeError('Native frame inspection did not return an image')
                download = await call('get_candidate_download', project_id=args.project_id, motion_id='walk', candidate_id=selected, format='png')
                report.update(selected_walk=selected, frame_geometry=download.get('frame_geometry'), image_blocks=sum(b.type == 'image' for b in frames.content))
                if args.download_dir:
                    async with httpx.AsyncClient(timeout=60) as client:
                        response = await client.get(download['upload_url'])
                        response.raise_for_status()
                    if not response.content.startswith(b'\x89PNG\r\n\x1a\n'):
                        raise RuntimeError('Download did not return a PNG')
                    target = args.download_dir / (selected + '.png')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(response.content)
                    report.update(download_path=str(target.resolve()), download_sha256=hashlib.sha256(response.content).hexdigest())
            print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8766/mcp')
    parser.add_argument('--project-id')
    parser.add_argument('--download-dir', type=Path)
    asyncio.run(main(parser.parse_args()))
