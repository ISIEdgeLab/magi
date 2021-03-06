#!/usr/bin/env python

from magi.util import helpers, querytool
import datetime
import logging
import optparse
import signal
import sys
import time

def _color(code, s):
    return '\033[%d;1m%s\033[0m' % (code, s)

def _red(s):
    return _color(31, s)

def _green(s):
    return _color(32, s)

def _yellow(s):
    return _color(33, s)

if __name__ == '__main__':
    optparser = optparse.OptionParser()
    
    optparser.add_option("-b", "--bridge", dest="bridge", help="Address of the bridge node to join the experiment overlay (ex: control.exp.proj)")
    optparser.add_option("-t", "--tunnel", dest="tunnel", action="store_true", default=False, help="Tell the tool to tunnel request through Deter Ops (users.deterlab.net).")
    optparser.add_option("-c", "--threshold", dest="threshold", type="float", default=50, help="CPU threshold")
    
    (options, args) = optparser.parse_args()
    
    if options.bridge is None:
        optparser.print_help()
        sys.exit(2)
    
    # Terminate if the user presses ctrl+c 
    signal.signal(signal.SIGINT, signal.SIG_DFL ) 
    
    try:
        tunnel_cmd = None
        if options.tunnel:
            tunnel_cmd = helpers.createSSHTunnel('users.deterlab.net', 18808, options.bridge, 18808, options.username)
            bridge = '127.0.0.1'
            logging.info('Tunnel setup done')
        else:
            bridge = options.bridge
            
        msgdest = options.bridge.split(".")[0]
        agents = ['processstats']
        processInfo = dict()
        
        lasttime = time.time() - 2
        
        while True:
            
            now = time.time()
            timestampChunks = [(lasttime, now)]
            lasttime = now
#            print timestampChunks
            
            data = querytool.getData(agents=agents, 
                                         nodes=None, 
                                         filters={},
                                         timestampChunks=timestampChunks, 
                                         bridge=bridge, 
                                         msgdest=msgdest)
            
            expstats = data['processstats']
            
#            print "expstats............"
#            print timestampChunks
#            print expstats
            
            dataFlag = False
            flag = False
            
            for node in expstats.keys():
                nodestats = expstats[node]
                for processstat in nodestats:
                    dataFlag = True
                    if (processstat['cpu_usage'] > options.threshold):
                        flag = True
                        processName = "Not Known"
                        if not node in processInfo:
                            processInfo[node] = querytool.getAgentsProcessInfo(node=node, bridge=bridge, msgdest=msgdest)
                        nodeProcessInfo = processInfo[node]
                        for entry in nodeProcessInfo:
                            if entry['processId'] == processstat['process_id'] and entry.get('threadId', -1) == processstat.get('thread_id', -1):
                                processName = entry['name']
                                break
            
                        print _red("Node %s, Process Id: %d, Thread Id: %d, Process Name: %s, CPU: %f, Time: %s" % (node, 
                                                                                                               processstat['process_id'], 
                                                                                                               processstat.get('thread_id', -1), 
                                                                                                               processName, 
                                                                                                               processstat['cpu_usage'], 
                                                                                                               datetime.datetime.fromtimestamp(float(processstat['created'])).strftime('%Y-%m-%d %H:%M:%S')))
            if not dataFlag:
                print _yellow("No data received.")        
            elif not flag:
                print _green("All processes are doing fine.")
                
            time.sleep(2)

    finally:
        if tunnel_cmd:
            logging.info("Closing tunnel")
            helpers.terminateProcess(tunnel_cmd)
    