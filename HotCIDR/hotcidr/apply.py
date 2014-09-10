#!/usr/bin/python
import boto.ec2
import collections
import sys

from hotcidr import fetch
from hotcidr import util

class Action(object):
    def __call__(self):
        raise NotImplementedError

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __hash__(self):
        return tuple(self.__dict__.items()).__hash__()

class CreateSecurityGroup(Action):
    def __init__(self, name, desc):
        self.name = name
        self.desc = desc

    def __call__(self, conn):
        conn.create_security_group(self.name, self.desc)

    def __repr__(self):
        return "Create new security group %s (%s)" % (self.name, self.desc)

class ModifyInstanceAttribute(Action):
    def __init__(self, inst_id, attr, value):
        self.inst_id = inst_id
        self.attr = attr
        self.value = value

    def __call__(self, conn):
        conn.modify_instance_attribute(self.inst_id, self.attr, self.value)

    def __repr__(self):
        return "Set %s of %s to %s" % (self.attr, self.inst_id, self.value)

class ModifyRule(Action):
    def __init__(self, group, rule):
        self.group = group
        self.rule = rule

    def __call__(self, conn, f):
        proto = self.rule.protocol
        if proto == 'all':
            proto = -1

        loc = self.rule.location
        if loc == 'all':
            loc = '0.0.0.0/0'

        if not self.rule.ports:
            fromport_temp = -1
            toport_temp = -1
        else:
            fromport_temp = self.rule.ports.fromport
            toport_temp = self.rule.ports.toport

        groupids = util.get_sgid(conn, self.group)
        for groupid in groupids:
            if util.is_cidr(loc):
                f(group_id=groupid,
                  ip_protocol=proto,
                  from_port=fromport_temp,
                  to_port=toport_temp,
                  cidr_ip=loc)

            else:
                locs = util.get_sgid(conn, loc)
                for loc in locs:
                    #Boto uses src_group_id or src_security_group_group_id to mean the
                    #same thing depending on which function f is used here.
                    try:
                        f(group_id=groupid,
                          ip_protocol=proto,
                          from_port=fromport_temp,
                          to_port=toport_temp,
                          src_group_id=loc)

                    except:
                        f(group_id=groupid,
                          ip_protocol=proto,
                          from_port=fromport_temp,
                          to_port=toport_temp,
                          src_security_group_group_id=loc)

class RemoveRule(ModifyRule):
    def __call__(self, conn):
        if self.rule.direction == 'inbound':
            f = conn.revoke_security_group
        elif self.rule.direction == 'outbound':
            f = conn.revoke_security_group_egress
        else:
            raise Exception("Invalid direction %s" % self.rule.direction)

        super(RemoveRule, self).__call__(conn, f)

    def __repr__(self):
        return "Del rule (%s, %s, %s) from %s" % (
                self.rule.protocol, self.rule.ports,
                self.rule.location, self.group)

class AddRule(ModifyRule):
    def __call__(self, conn):
        if self.rule.direction == 'inbound':
            f = conn.authorize_security_group
        elif self.rule.direction == 'outbound':
            f = conn.authorize_security_group_egress
        else:
            raise Exception("Invalid direction %s" % self.rule.direction)

        super(AddRule, self).__call__(conn, f)

    def __repr__(self):
        return "Add rule (%s, %s, %s) to %s" % (
                self.rule.protocol, self.rule.ports,
                self.rule.location, self.group)

rule_attr = ('direction', 'location', 'protocol', 'ports')
Rule = collections.namedtuple('Rule', rule_attr)
def rules(group):
    if 'rules' in group:
        for rule in group['rules']:
            r = {k: rule[k] for k in rule_attr if k in rule}
            for attr in rule_attr:
                r.setdefault(attr, None)
            yield Rule(**r)

def get_actions(git_dir, aws_dir):
    aws_instances = util.load_boxes(aws_dir)
    aws_groups = util.load_groups(aws_dir)

    git_instances = util.load_boxes(git_dir)
    git_groups = util.load_groups(git_dir)

    # Add missing groups to AWS
    for g in git_groups:
        if g not in aws_groups:
            print("Adding group %s to AWS" % g)
            desc = 'Automatically created by HotCIDR'
            if 'description' in git_groups[g]:
                desc = git_groups[g]['description']
            yield CreateSecurityGroup(g, desc)

    # Update associated security groups for instances
    for aws_id, aws_inst in aws_instances.items():
        if aws_id in git_instances and 'groups' in git_instances[aws_id]:
            groups = git_instances[aws_id]['groups']
            if set(groups) != set(aws_inst['groups']):
                groups = set(aws_groups[g]['id'] for g in groups
                                                 if 'id' in aws_groups[g])
                yield ModifyInstanceAttribute(aws_id, 'groupSet', groups)
        else:
            print("Skipping instance %s (Does not exist in AWS)" % aws_id)

    #TODO: Delete security groups that are unused

    # Update rules for each security group
    for g, git_group in git_groups.items():
        git_rules = set(rules(git_group))

        if g in aws_groups:
            aws_rules = set(rules(aws_groups[g]))
        else:
            aws_rules = set()

        if git_rules != aws_rules:
            for rule in aws_rules - git_rules:
                yield RemoveRule(g, rule)
            for rule in git_rules - aws_rules:
                yield AddRule(g, rule)

class Changes(object):
    def __init__(self, actions=None):
        self.created_security_groups = 0
        self.modified_instance_attributes = 0
        self.added_rules = 0
        self.removed_rules = 0

        if actions:
            for action in actions:
                self.count(action)

    def count(self, action):
        assert(isinstance(action, Action))
        if isinstance(action, CreateSecurityGroup):
            self.created_security_groups += 1
        elif isinstance(action, ModifyInstanceAttribute):
            self.modified_instance_attributes += 1
        elif isinstance(action, AddRule):
            self.added_rules += 1
        elif isinstance(action, RemoveRule):
            self.removed_rules += 1

    def __repr__(self):
        r = []
        if self.created_security_groups:
            r.append("%d group(s) created" % self.created_security_groups)
        if self.modified_instance_attributes:
            r.append("%d instance(s) updated" % self.modified_instance_attributes)
        if self.added_rules:
            r.append("%d rule(s) added" % self.added_rules)
        if self.removed_rules:
            r.append("%d rule(s) removed" % self.removed_rules)
        if not r:
            return "No changes"
        return ", ".join(r)

def main(git_repo, region_code, aws_key, aws_secret, dry_run):
    with fetch.vpc(region_code, aws_key, aws_secret) as aws_dir,\
         util.repo(git_repo) as git_dir:
        actions = list(get_actions(git_dir, aws_dir))

        conn = boto.ec2.connect_to_region(region_code,
                                          aws_access_key_id=aws_key,
                                          aws_secret_access_key=aws_secret)

        count = len(actions)
        changes = []
        for num, action in enumerate(actions, 1):
            print("Action %d/%d: %s" % (num, count, action))
            sys.stdout.flush()
            if not dry_run:
                action(conn)

        print(Changes(actions))
