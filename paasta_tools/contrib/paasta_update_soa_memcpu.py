#!/usr/bin/env python3
import argparse
import contextlib
import json
import os
import subprocess
import tempfile
import time

import requests
import ruamel.yaml as yaml


def parse_args(argv):
    parser = argparse.ArgumentParser(description='')
    parser.add_argument(
        '-s',
        '--splunk-creds',
        help='Creds for Splunk API, user:pass',
        dest='splunk_creds',
        required=True,
    )
    parser.add_argument(
        '-j',
        '--jira-creds',
        help='Creds for JIRA API, user:pass',
        dest='jira_creds',
        required=False,
    )
    parser.add_argument(
        '-t',
        '--create-tickets',
        help='Do not create JIRA tickets',
        action='store_true',
        dest='ticket',
        default=False,
    )
    parser.add_argument(
        '-r',
        '--publish-reviews',
        help='Guess owners and publish reviews automatically',
        action='store_true',
        dest='publish_reviews',
        default=False,
    )
    parser.add_argument(
        '-b',
        '--bulk',
        help='Patch all services with only one code review',
        action='store_true',
        dest='bulk',
        default=False,
    )
    parser.add_argument(
        '-c',
        '--cpu-report-csv',
        help='Splunk csv file from which to pull data. (cpus)',
        required=False,
        dest='cpu_report_csv',
    )
    parser.add_argument(
        '-m',
        '--memory-report-csv',
        help='Splunk csv file from which to pull data. (memory)',
        dest='mem_report_csv',
        required=False,
    )
    parser.add_argument(
        '-y',
        '--yelpsoa-configs-dir',
        help='Use provided existing yelpsoa-configs instead of cloning the repo in a temporary dir. Only avail with -b option',
        dest='YELPSOA_DIR',
        required=False,
    )

    return parser.parse_args(argv)


def tempdir():
    return tempfile.TemporaryDirectory(prefix='repo', dir='/nail/tmp')


@contextlib.contextmanager
def cwd(path):
    pwd = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(pwd)


@contextlib.contextmanager
def in_tempdir():
    with tempdir() as tmp:
        print('Working directory: {}'.format(tmp))
        with cwd(tmp):
            yield


def get_report_from_splunk(creds, filename, resource_type):
    url = 'https://splunk-api.yelpcorp.com/servicesNS/nobody/yelp_performance/search/jobs/export'
    search = (
        '| inputlookup {} |'
        ' eval _time = search_time | where _time > relative_time(now(),"-7d")'
    ).format(filename)
    data = {'output_mode': 'json', 'search': search}
    creds = creds.split(':')
    resp = requests.post(url, data=data, auth=(creds[0], creds[1]))
    resp_text = resp.text.split('\n')
    resp_text = [x for x in resp_text if x]
    resp_text = [json.loads(x) for x in resp_text]
    services_to_update = {}
#    import pdb; pdb.set_trace()
    for d in resp_text:
        if not 'result' in d:
            raise ValueError("Splunk request didn't return any results")
        criteria = d['result']['criteria']
        serv = {}
        serv['service'] = criteria.split(' ')[0]
        serv['cluster'] = criteria.split(' ')[1]
        serv['instance'] = criteria.split(' ')[2]
        serv['owner'] = d['result']['service_owner']
        serv['date'] = d['result']['_time'].split(' ')[0]
        serv['money'] = d['result'].get('estimated_monthly_savings', 0)
        serv['project'] = d['result'].get('project', 'Unavailable')
        if resource_type == 'cpu':
            serv['cpus'] = d['result']['suggested_cpus']
            serv['old_cpus'] = d['result']['current_cpus']
        if resource_type == 'mem':
            serv['mem'] = d['result']['suggested_mem']
            serv['old_mem'] = d['result']['mem']
        services_to_update[criteria]=serv

    return services_to_update


def clone(target_dir):
    remote = 'git@sysgit.yelpcorp.com:yelpsoa-configs'
    subprocess.check_call(('git', 'clone', remote, target_dir))

def create_branch(branch_name):
    subprocess.check_call(('git', 'checkout', '-b', branch_name))


def bulk_commit(filenames):
    message = 'Rightsizer bulk update'
    subprocess.check_call(['git', 'add'] + filenames)
    subprocess.check_call(('git', 'commit', '-n', '-m', message))

def bulk_review(filenames):
    reviewers = get_reviewers_in_group('right-sizer')
    reviewers_arg = ' '.join(reviewers)
    summary = 'Rightsizer bulk update'
    description = 'Please review changes carefully'
    subprocess.check_call(
        (
            'review-branch',
            f'--summary={summary}',
            f'--description={description}',
            '--reviewers',
            reviewers_arg,
            '--server',
            'https://reviewboard.yelpcorp.com',
        )
    )

def commit(filename, serv):
    message = 'Updating {} for {}provisioned cpu from {} to {} cpus'.format(
        filename, serv['state'], serv['old_cpus'], serv['cpus']
    )
    subprocess.check_call(('git', 'add', filename))
    subprocess.check_call(('git', 'commit', '-n', '-m', message))


def get_reviewers_in_group(group_name):
    '''Using rbt's target-groups argument overrides our configured default review groups.
    So we'll expand the group into usernames and pass those users in the group individually.
    '''
    rightsizer_reviewers = json.loads(
        subprocess.check_output(
            (
                'rbt',
                'api-get',
                '--server',
                'https://reviewboard.yelpcorp.com',
                f'groups/{group_name}/users/',
            )
        ).decode('UTF-8')
    )
    return [user.get('username', '') for user in rightsizer_reviewers.get('users', {})]


def get_reviewers(filename):
    recent_authors = set()
    authors = (
        subprocess.check_output(('git', 'log', '--format=%ae', '--', filename))
        .decode('UTF-8')
        .splitlines()
    )

    authors = [x.split('@')[0] for x in authors]
    for author in authors:
        recent_authors.add(author)
        if len(recent_authors) >= 3:
            break
    return recent_authors


def review(filename, summary, description, publish_reviews):
    all_reviewers = get_reviewers(filename).union(get_reviewers_in_group('right-sizer'))
    reviewers_arg = ' '.join(all_reviewers)
    if publish_reviews:
        subprocess.check_call(
            (
                'review-branch',
                f'--summary={summary}',
                f'--description={description}',
                '-p',
                '--reviewers',
                reviewers_arg,
                '--server',
                'https://reviewboard.yelpcorp.com',
            )
        )
    else:
        subprocess.check_call(
            (
                'review-branch',
                f'--summary={summary}',
                f'--description={description}',
                '--reviewers',
                reviewers_arg,
                '--server',
                'https://reviewboard.yelpcorp.com',
            )
        )


def edit_soa_configs(filename, instance, cpu, mem):
    if not os.path.exists(filename):
        filename=filename.replace('marathon', 'kubernetes')
    try:
        with open(filename, 'r') as fi:
            yams = fi.read()
            yams = yams.replace('cpus: .', 'cpus: 0.')
            data = yaml.round_trip_load(yams, preserve_quotes=True)

        instdict = data[instance]
        if cpu:
            instdict['cpus'] = float(cpu)
        if mem:
            mem = round(float(mem))
            if mem > 0:
                instdict['mem'] = round(float(mem))
        out = yaml.round_trip_dump(data, width=120)

        with open(filename, 'w') as fi:
            fi.write(out)
    except FileNotFoundError:
        print('Could not find {}'.format(filename))
    except KeyError:
        print('Error in {}'.format(filename))

def create_jira_ticket(serv, creds, description):
    creds = creds.split(':')
    options = {'server': 'https://jira.yelpcorp.com'}
    jira_cli = JIRA(options=options, basic_auth=(creds[0], creds[1]))
    jira_ticket = {}
    # Sometimes a project has required fields we can't predict
    try:
        jira_ticket = {
            'project': {'key': serv['project']},
            'description': description,
            'issuetype': {'name': 'Improvement'},
            'labels': ['perf-watching', 'paasta-rightsizer'],
            'summary': '{s}.{i} in {c} may be {o}provisioned'.format(
                s=serv['service'],
                i=serv['instance'],
                c=serv['cluster'],
                o=serv['state'],
            ),
        }
        tick = jira_cli.create_issue(fields=jira_ticket)
    except Exception:
        jira_ticket['project'] = {'key': 'PEOBS'}
        jira_ticket['labels'].append(serv['service'])
        tick = jira_cli.create_issue(fields=jira_ticket)
    return tick.key


def _get_dashboard_qs_param(param, value):
    # Some dashboards may ask for query string params like param=value, but not this provider.
    return f'variables%5B%5D={param}%3D{param}:{value}'


def generate_ticket_content(serv):
    cpus = float(serv['cpus'])
    provisioned_state = 'over'
    if cpus > float(serv['old_cpus']):
        provisioned_state = 'under'

    serv['state'] = provisioned_state
    ticket_desc = (
        'This ticket and CR have been auto-generated to help keep PaaSTA right-sized.'
        '\nPEOBS will review this CR and give a shipit. Then an ops deputy from your team can merge'
        ' if these values look good for your service after review.'
        '\nOpen an issue with any concerns and someone from PEOBS will respond.'
        '\nWe suspect that {s}.{i} in {c} may have been {o}-provisioned'
        ' during the 1 week prior to {d}. It initially had {x} cpus, but based on the below dashboard,'
        ' we recommend {y} cpus.'
        '\n- Dashboard: https://y.yelpcorp.com/{o}provisioned?{cluster_param}&{service_param}&{instance_param}'
        '\n- Service owner: {n}'
        '\n- Estimated monthly excess cost: ${m}'
        '\n\nFor more information and sizing examples for larger services:'
        '\n- Runbook: https://y.yelpcorp.com/rb-provisioning-alert'
        '\n- Alert owner: pe-observability@yelp.com'
    ).format(
        s=serv['service'],
        c=serv['cluster'],
        i=serv['instance'],
        o=provisioned_state,
        d=serv['date'],
        n=serv['owner'],
        m=serv['money'],
        x=serv['old_cpus'],
        y=serv['cpus'],
        cluster_param=_get_dashboard_qs_param(
            'paasta_cluster', serv['cluster'].replace('marathon-', '')
        ),
        service_param=_get_dashboard_qs_param('paasta_service', serv['service']),
        instance_param=_get_dashboard_qs_param('paasta_instance', serv['instance']),
    )
    summary = f"Rightsizing {serv['service']}.{serv['instance']} in {serv['cluster']} to make it not have {provisioned_state}-provisioned cpu"  # noqa: E501
    return (summary, ticket_desc)

def bulk_rightsize(report, working_dir):
    with cwd(working_dir):
        branch = 'rightsize-bulk-{}'.format(int(time.time()))
        create_branch(branch)
        filenames=[]
        for _, serv in report.items():
            filename = '{}/{}.yaml'.format(serv['service'], serv['cluster'])
            filenames.append(filename)
            cpus=serv.get('cpus', None)
            mem=serv.get('mem', None)
            edit_soa_configs(filename, serv['instance'], cpus, mem)
        bulk_commit(filenames)

def individual_rightsize(report):
    for serv in cpu_report:
        filename = '{}/{}.yaml'.format(serv['service'], serv['cluster'])
        summary, ticket_desc = generate_ticket_content(serv)

        if args.ticket:
            branch = create_jira_ticket(serv, args.jira_creds, ticket_desc)
        else:
            branch = 'rightsize-{}'.format(int(time.time()))

        with in_tempdir():
            clone(branch)
            cpus=serv.get('cpus', None)
            mem=serv.get('mem', None)
            edit_soa_configs(filename, serv['instance'], cpus, mem)
            try:
                commit(filename, serv)
                review(filename, summary, ticket_desc, args.publish_reviews)
            except Exception:
                print(
                    (
                        '\nUnable to push changes to {f}. Check if {f} conforms to'
                        'yelpsoa-configs yaml rules. No review created. To see the'
                        'cpu suggestion for this service check {t}.'
                    ).format(f=filename, t=branch)
                )
                continue

def main(argv=None):
    args = parse_args(argv)
    if args.ticket:
        if not args.jira_creds:
            raise ValueError('No JIRA creds specified')
        # Only import the jira module if we need too
        from jira.client import JIRA

    if not ( args.cpu_report_csv or args.mem_report_csv ):
        raise ValueError('Need at least a CPU or memory report to work on')
    # CPU and Memory report can come in two different reports. Let's combine them
    combined_report = {}
    if args.cpu_report_csv:
        combined_report.update(get_report_from_splunk(args.splunk_creds, args.cpu_report_csv, 'cpu'))
    if args.mem_report_csv:
        print(args.mem_report_csv)
        combined_report.update(get_report_from_splunk(args.splunk_creds, args.mem_report_csv, 'mem'))

    if args.bulk:
        if args.YELPSOA_DIR:
            working_dir=args.YELPSOA_DIR
        else:
            working_dir=tempdir()
            clone(working_dir)

        bulk_rightsize(combined_report, working_dir)

    else:
        individual_rightsize(combined_report)


if __name__ == '__main__':
    main()
