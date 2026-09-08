import re

with open('server.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
status_task_var = None
for i, line in enumerate(lines):
    new_lines.append(line)
    if 'global_conn = conn' in line:
        # Insert status sender task
        indent = line[:len(line) - len(line.lstrip())]
        new_lines.append(indent + '# Start background task for sub-agent status updates\n')
        new_lines.append(indent + 'async def status_sender():\n')
        new_lines.append(indent + '    while True:\n')
        new_lines.append(indent + '        events = await runtime.task_manager.consume_status_events()\n')
        new_lines.append(indent + '        for event in events:\n')
        new_lines.append(indent + '            await conn.send_json({\n')
        new_lines.append(indent + '                "type": "subagent_status",\n')
        new_lines.append(indent + '                "task_id": event.task_id,\n')
        new_lines.append(indent + '                "agent_name": event.agent_name,\n')
        new_lines.append(indent + '                "status": event.status,\n')
        new_lines.append(indent + '                "task_description": event.task_description,\n')
        new_lines.append(indent + '                "result": event.result,\n')
        new_lines.append(indent + '                "progress": event.progress,\n')
        new_lines.append(indent + '            })\n')
        new_lines.append(indent + '        await asyncio.sleep(0.1)  # Small delay to avoid busy loop\n')
        new_lines.append(indent + 'status_task = asyncio.create_task(status_sender())\n')
        status_task_var = 'status_task'
    if 'except WebSocketDisconnect:' in line and status_task_var:
        # Insert cancel before this line
        indent = line[:len(line) - len(line.lstrip())]
        new_lines.insert(-1, indent + f'{status_task_var}.cancel()\n')

with open('server.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
