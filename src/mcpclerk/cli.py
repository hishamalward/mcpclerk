"""Manage native MCP configuration and evaluate task-scoped calls."""
import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, hosts, judge, storage


def parser():
    cli = argparse.ArgumentParser(prog='mcpclerk', description='One management plane for Claude Code and Codex MCP configuration.')
    cli.add_argument('--version', action='version', version='mcpclerk ' + __version__)
    commands = cli.add_subparsers(dest='action', required=True)
    ls = commands.add_parser('list', help='passive inventory with provenance; does not start servers')
    ls.add_argument('--host', choices=('claude', 'codex'))
    ls.add_argument('--project')
    for action in ('add', 'remove', 'enable', 'disable', 'tool'):
        cmd = commands.add_parser(action, help=f'{action} native host configuration with an undo receipt')
        cmd.add_argument('name')
        cmd.add_argument('--host', required=True, choices=('claude', 'codex'))
        cmd.add_argument('--scope', required=True, choices=('user', 'project', 'local'))
        cmd.add_argument('--project')
        cmd.add_argument('--plugin', help='Codex plugin ID for native server/tool policy')
        if action == 'add':
            cmd.add_argument('--file', required=True, help='JSON file containing one native server definition')
        if action == 'tool':
            cmd.add_argument('tool')
            cmd.add_argument('--mode', required=True, choices=('allow', 'ask', 'deny', 'clear'))
    for action in ('catalog', 'migrate-keel'):
        cmd = commands.add_parser(action, help='explicitly replace one catalog layer with a reversible write')
        cmd.add_argument('--scope', required=True, choices=('user', 'project'))
        cmd.add_argument('--project')
        cmd.add_argument('--file', required=True)
    undo = commands.add_parser('undo', help='restore exact prior bytes if the file has not changed')
    undo.add_argument('transaction')
    commands.add_parser('judge', help='read a version-1 call from JSON stdin; emit one decision')
    return cli


def input_file(path):
    try:
        data = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, UnicodeError):
        raise ValueError('input JSON file is unavailable or malformed') from None
    if not isinstance(data, dict):
        raise ValueError('input JSON must contain an object')
    return data


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'judge':
            try:
                payload = json.load(sys.stdin)
            except (ValueError, UnicodeError):
                raise ValueError('judge input is malformed JSON') from None
            result = judge.evaluate(payload)
        elif args.action == 'list':
            project = str(config.project_path(args.project)) if args.project else None
            rows = hosts.inventory(project)
            result = {'servers': [r for r in rows if not args.host or r['host'] == args.host],
                      'state': 'configured; host trust, managed policy, session flags and connections are not observed'}
        elif args.action == 'undo':
            result = {'restored': storage.undo(args.transaction)}
        else:
            if args.action in ('catalog', 'migrate-keel'):
                fn = config.set_catalog if args.action == 'catalog' else config.migrate_keel
                transaction = fn(input_file(args.file), args.scope, args.project)
            else:
                transaction = config.change(args.action, args.name, args.host, args.scope, args.project,
                                            entry=input_file(args.file) if args.action == 'add' else None,
                                            tool=getattr(args, 'tool', None), mode=getattr(args, 'mode', None),
                                            plugin=args.plugin)
            result = {'transaction': transaction, 'state': 'configuration updated',
                      'note': 'Reload the host. Inherited, managed and wildcard restrictions may still apply; project trust is separate.'}
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError) as error:
        # Native parsers and transport definitions can carry secrets; only our validation
        # messages are emitted. Raw OS errors are deliberately not included.
        message = str(error) if isinstance(error, ValueError) else 'configuration operation failed'
        print('mcpclerk: ' + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
