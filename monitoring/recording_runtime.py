"""Generate the approved rules and exec official vmalert; no replay supervisor."""
import argparse
import json
import os
from pathlib import Path
from .native_rules import build


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen',default='127.0.0.1:18432')
    parser.add_argument('--vm',default=os.environ.get('VM_URL','http://127.0.0.1:18428'))
    parser.add_argument('--binary',default='/vmalert-prod')
    parser.add_argument('--output',default='/tmp/monitoring-native-rules.json')
    parser.add_argument('--rules-file', help='Read controller-generated rules; never overwrite this file')
    parser.add_argument('--replay-start')
    parser.add_argument('--replay-end')
    args=parser.parse_args()
    if bool(args.replay_start)!=bool(args.replay_end):parser.error('Both replay bounds are required')
    if args.rules_file:
        if not Path(args.rules_file).is_file():parser.error('Controller rules file is missing')
        rules_file=args.rules_file
    else:
        config,_=build()
        Path(args.output).write_text(json.dumps(config,indent=2)+'\n')
        rules_file=args.output
    command=[args.binary,'-rule='+rules_file,'-datasource.url='+args.vm,
        '-remoteWrite.url='+args.vm,'-httpListenAddr='+args.listen,
        '-datasource.queryStep=0','-remoteWrite.flushInterval=1s']
    if args.replay_start:
        command+=['-replay.timeFrom='+args.replay_start,'-replay.timeTo='+args.replay_end,
                  '-replay.rulesDelay=0','-replay.maxDatapointsPerQuery=60']
    os.execv(args.binary,command)


if __name__=='__main__':main()
