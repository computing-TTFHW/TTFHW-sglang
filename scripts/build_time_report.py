#!/usr/bin/env python3
"""
Build Time Report Generator

Parses GitHub Actions workflow logs to extract timing information
for workflow steps and Dockerfile build stages.
Generates JSON and HTML reports.
"""

import os
import json
import re
import requests
from datetime import datetime
from collections import defaultdict


# ANSI escape code pattern
ANSI_PATTERN = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return ANSI_PATTERN.sub('', text)


def parse_time(time_str):
    """Parse ISO format time string to datetime object."""
    if time_str and time_str != 'unknown':
        try:
            return datetime.fromisoformat(time_str.replace('Z', '+00:00'))
        except:
            return None
    return None


def format_duration(seconds):
    """Format duration in human-readable format."""
    if seconds is None:
        return "N/A"
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.1f}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def parse_dockerfile_log(log_content):
    """
    Parse Buildkit log to extract Dockerfile stage timings.

    Extracts timing for each Dockerfile layer including:
    - FROM: Base image pull
    - RUN: Command execution
    - COPY/ADD: File operations
    - ENV/ARG/LABEL: Metadata operations
    - WORKDIR: Directory operations
    - EXPOSE/CMD/ENTRYPOINT: Container configuration

    Buildkit log format:
    #1 [internal] load build definition from Dockerfile
    #1 DONE 0.5s

    #2 [linux/amd64 internal] load metadata for ...
    #2 DONE 1.2s

    #3 [1/8] FROM docker.io/python:3.11-slim
    #3 DONE 10.5s

    #4 [2/8] RUN apt-get update
    #4 0.123 Getting: http://...
    #4 1.234 Get:1 http://...
    #4 DONE 15.5s
    """
    stages = []
    stage_starts = {}
    completed_stages = set()

    # Pattern for buildkit stage start: #N [...] command
    # Matches lines like:
    #   #1 [internal] load build definition from Dockerfile
    #   #2 [linux/amd64 internal] load metadata for ...
    #   #3 [1/8] FROM docker.io/python:3.11-slim
    #   #3 [2/8] RUN apt-get update
    stage_pattern = r'^#(\d+)\s+\[([^\]]*)\]\s*(.+)'

    # Pattern for DONE line: #N DONE 1.234s
    done_pattern = r'^#(\d+)\s+DONE\s+(\d+\.\d+s)'

    # Pattern to identify Dockerfile instruction type from stage info like [1/8] FROM ...
    dockerfile_instructions = {
        'FROM': r'^FROM\s+',
        'RUN': r'^RUN\s+',
        'COPY': r'^COPY\s+',
        'ADD': r'^ADD\s+',
        'ENV': r'^ENV\s+',
        'ARG': r'^ARG\s+',
        'LABEL': r'^LABEL\s+',
        'WORKDIR': r'^WORKDIR\s+',
        'EXPOSE': r'^EXPOSE\s+',
        'CMD': r'^CMD\s+',
        'ENTRYPOINT': r'^ENTRYPOINT\s+',
        'USER': r'^USER\s+',
        'VOLUME': r'^VOLUME\s+',
        'SHELL': r'^SHELL\s+',
    }

    lines = log_content.split('\n')
    print(f"    Parsing {len(lines)} lines of log...")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check for DONE first (must happen after stage start)
        done_match = re.search(done_pattern, line)
        if done_match:
            stage_num = done_match.group(1)
            duration_str = done_match.group(2)
            duration_sec = float(duration_str.replace('s', ''))

            if stage_num in stage_starts and stage_num not in completed_stages:
                stage_data = stage_starts[stage_num]
                stages.append({
                    'stage_id': f"#{stage_num}",
                    'stage_info': stage_data['stage_info'],
                    'command': stage_data['command'],
                    'instruction_type': stage_data['instruction_type'],
                    'instruction_detail': stage_data['instruction_detail'],
                    'duration': duration_sec,
                    'duration_formatted': format_duration(duration_sec)
                })
                completed_stages.add(stage_num)
            continue

        # Check for stage start with [...] bracket
        stage_match = re.search(stage_pattern, line)
        if stage_match:
            stage_num = stage_match.group(1)
            stage_info = stage_match.group(2).strip()
            stage_cmd = stage_match.group(3).strip()

            # Skip if we already have a DONE for this stage
            if stage_num in completed_stages:
                continue

            # Skip if this is a continuation line (starts with stage_num followed by space and number)
            # e.g., "#3 0.123 resolve ..." - this is a sub-line, not a new stage
            if re.match(r'^#\d+\s+\d+\.\d+', line):
                continue

            # Identify Dockerfile instruction type from stage_info
            # stage_info might be like "1/8" or "linux/amd64 internal" or "2/8] FROM ..."
            instruction_type = 'OTHER'
            instruction_detail = stage_cmd[:150]

            # Check if stage_info contains instruction like [2/8] RUN ...
            for instr_type, pattern in dockerfile_instructions.items():
                # Check in stage_info (e.g., "2/8] FROM docker.io/...")
                if re.search(pattern, stage_info, re.IGNORECASE):
                    instruction_type = instr_type
                    match = re.search(pattern + r'(\S+)?', stage_info, re.IGNORECASE)
                    if match and match.group(1):
                        instruction_detail = match.group(1)[:150]
                    else:
                        instruction_detail = stage_info[:150]
                    break

            # Also check stage_cmd if no instruction found
            if instruction_type == 'OTHER':
                for instr_type, pattern in dockerfile_instructions.items():
                    if re.search(pattern, stage_cmd, re.IGNORECASE):
                        instruction_type = instr_type
                        instruction_detail = stage_cmd[:150]
                        break

            # Handle special cases
            if 'internal' in stage_info.lower():
                instruction_type = 'INTERNAL'
                instruction_detail = stage_info

            stage_starts[stage_num] = {
                'stage_info': stage_info,
                'command': stage_cmd[:150],
                'instruction_type': instruction_type,
                'instruction_detail': instruction_detail
            }

    print(f"    Found {len(stages)} completed Dockerfile stages")
    return stages


def analyze_dockerfile_stages(stages):
    """
    Analyze Dockerfile stages and group by instruction type.

    Returns statistics about each instruction type.
    """
    by_type = defaultdict(list)
    for stage in stages:
        instr_type = stage.get('instruction_type', 'OTHER')
        by_type[instr_type].append(stage)

    analysis = {}
    for instr_type, type_stages in by_type.items():
        total_duration = sum(s.get('duration', 0) for s in type_stages)
        analysis[instr_type] = {
            'count': len(type_stages),
            'total_duration': total_duration,
            'total_duration_formatted': format_duration(total_duration),
            'avg_duration': total_duration / len(type_stages) if type_stages else 0,
            'stages': type_stages
        }

    return analysis


def generate_build_report(gh_token, run_id, repo, output_dir='.'):
    """Generate build time report from GitHub Actions workflow run."""

    headers = {
        'Authorization': f'token {gh_token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    # Get workflow run info
    run_url = f'https://api.github.com/repos/{repo}/actions/runs/{run_id}'
    run_resp = requests.get(run_url, headers=headers)
    run_resp.raise_for_status()
    run_data = run_resp.json()

    # Get all jobs for this run (with pagination support)
    all_jobs = []
    jobs_url = f'https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs'

    print(f"Fetching jobs for run {run_id}...")
    print(f"  API URL: {jobs_url}")

    while jobs_url:
        jobs_resp = requests.get(jobs_url, headers=headers)
        jobs_resp.raise_for_status()
        jobs_data = jobs_resp.json()
        new_jobs = jobs_data.get('jobs', [])
        print(f"  Found {len(new_jobs)} jobs in this page")
        all_jobs.extend(new_jobs)
        # Handle pagination
        jobs_url = jobs_resp.links.get('next', {}).get('url')

    print(f"Total jobs to process: {len(all_jobs)}")

    # Print job names for debugging
    for job in all_jobs:
        print(f"  - Job: {job.get('name')} (ID: {job.get('id')}, Status: {job.get('status')})")

    build_report = {
        'workflow_name': run_data.get('name', 'Unknown'),
        'run_id': run_id,
        'workflow_run_url': run_data.get('html_url', ''),
        'trigger': run_data.get('event', 'workflow_dispatch'),
        'branch': run_data.get('head_branch', 'unknown'),
        'commit': run_data.get('head_sha', 'unknown'),
        'created_at': run_data.get('created_at', 'unknown'),
        'updated_at': run_data.get('updated_at', 'unknown'),
        'jobs': []
    }

    for job in all_jobs:
        # Skip jobs that are not completed
        if job.get('status') != 'completed':
            continue

        print(f"Processing job: {job.get('name', 'Unknown')} (ID: {job.get('id')})...")

        job_started = parse_time(job.get('started_at'))
        job_completed = parse_time(job.get('completed_at'))

        job_info = {
            'job_name': job.get('name', 'Unknown'),
            'job_id': job.get('id'),
            'status': job.get('status', 'unknown'),
            'conclusion': job.get('conclusion', 'unknown'),
            'started_at': job.get('started_at', 'unknown'),
            'completed_at': job.get('completed_at', 'unknown'),
            'steps': [],
            'dockerfile_stages': []
        }

        if job_started and job_completed:
            duration = (job_completed - job_started).total_seconds()
            job_info['duration_seconds'] = duration
            job_info['duration_formatted'] = format_duration(duration)

        # Get detailed job steps
        if job.get('id'):
            job_steps_url = f"https://api.github.com/repos/{repo}/actions/jobs/{job['id']}"
            steps_resp = requests.get(job_steps_url, headers=headers)
            if steps_resp.status_code == 200:
                job_detail = steps_resp.json()
                for idx, step in enumerate(job_detail.get('steps', [])):
                    step_info = {
                        'step_number': idx + 1,
                        'name': step.get('name', 'Unknown'),
                        'status': step.get('status', 'unknown'),
                        'conclusion': step.get('conclusion', 'unknown'),
                        'started_at': step.get('started_at', 'unknown'),
                        'completed_at': step.get('completed_at', 'unknown')
                    }

                    step_started = parse_time(step.get('started_at'))
                    step_completed = parse_time(step.get('completed_at'))
                    if step_started and step_completed:
                        step_duration = (step_completed - step_started).total_seconds()
                        step_info['duration_seconds'] = step_duration
                        step_info['duration_formatted'] = format_duration(step_duration)

                    job_info['steps'].append(step_info)

        # Get job logs for Dockerfile parsing
        if job.get('status') == 'completed' and job.get('log_url'):
            log_resp = requests.get(job['log_url'], headers=headers)
            if log_resp.status_code == 200:
                # GitHub Actions logs contain ANSI codes, strip them
                raw_log = log_resp.text
                log_content = strip_ansi(raw_log)
                print(f"  Parsing logs for job '{job['job_name']}' ({len(log_content)} bytes)...")
                dockerfile_stages = parse_dockerfile_log(log_content)
                job_info['dockerfile_stages'] = dockerfile_stages
                print(f"    Found {len(dockerfile_stages)} Dockerfile stages")

                # Save raw log for debugging
                safe_job_name = job['job_name'].replace('/', '-').replace('\\', '-').replace(' ', '_')
                log_file = f'logs/job-{safe_job_name}-{job.get("id", "unknown")}.log'
                os.makedirs(os.path.dirname(log_file), exist_ok=True)
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.write(log_content)
                print(f"    Log saved to {log_file}")

                # Save parsed stages for debugging
                if dockerfile_stages:
                    stages_file = f'logs/stages-{safe_job_name}-{job.get("id", "unknown")}.json'
                    with open(stages_file, 'w') as f:
                        json.dump(dockerfile_stages, f, indent=2, ensure_ascii=False)
                    print(f"    Stages saved to {stages_file}")

        build_report['jobs'].append(job_info)

    # Calculate summary statistics
    all_stages = [s for j in build_report['jobs'] for s in j.get('dockerfile_stages', [])]

    # Analyze Dockerfile stages by instruction type
    dockerfile_analysis = analyze_dockerfile_stages(all_stages)

    build_report['summary'] = {
        'total_jobs': len(build_report['jobs']),
        'successful_jobs': sum(1 for j in build_report['jobs'] if j.get('conclusion') == 'success'),
        'failed_jobs': sum(1 for j in build_report['jobs'] if j.get('conclusion') == 'failure'),
        'total_dockerfile_stages': len(all_stages),
        'slowest_stages': sorted(
            all_stages,
            key=lambda x: x.get('duration', 0),
            reverse=True
        )[:10],
        'dockerfile_analysis': dockerfile_analysis
    }

    # Save JSON report
    json_path = os.path.join(output_dir, 'build-report.json')
    with open(json_path, 'w') as f:
        json.dump(build_report, f, indent=2, ensure_ascii=False)

    print(f"JSON report generated: {json_path}")
    print(f"Total jobs: {build_report['summary']['total_jobs']}")
    print(f"Successful: {build_report['summary']['successful_jobs']}")
    print(f"Failed: {build_report['summary']['failed_jobs']}")

    return build_report


def generate_html_report(report, output_dir='.'):
    """Generate HTML visualization report from build report data."""

    # Load HTML template
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        'scripts',
        'report_templates',
        'report_template.html'
    )

    with open(template_path, 'r') as f:
        template = f.read()

    # Replace placeholders with actual data
    html = template
    html = html.replace('{{WORKFLOW_NAME}}', report['workflow_name'])
    html = html.replace('{{WORKFLOW_RUN_URL}}', report.get('workflow_run_url', '#'))
    html = html.replace('{{RUN_ID}}', str(report['run_id']))
    html = html.replace('{{TRIGGER}}', report['trigger'])
    html = html.replace('{{BRANCH}}', report['branch'])
    html = html.replace('{{COMMIT}}', report['commit'][:8])
    html = html.replace('{{CREATED_AT}}', report['created_at'])
    html = html.replace('{{UPDATED_AT}}', report['updated_at'])
    html = html.replace('{{SUCCESSFUL_JOBS}}', str(report['summary']['successful_jobs']))
    html = html.replace('{{FAILED_JOBS}}', str(report['summary']['failed_jobs']))
    html = html.replace('{{TOTAL_DOCKERFILE_STAGES}}', str(report['summary']['total_dockerfile_stages']))
    html = html.replace('{{TOTAL_JOBS}}', str(len(report['jobs'])))

    # Generate jobs HTML
    jobs_html = ""
    for job in report['jobs']:
        status_class = f"status-{job.get('conclusion', 'pending')}"
        status_text = job.get('conclusion', 'unknown')
        duration = job.get('duration_formatted', 'N/A')

        job_html = f'''
        <div class="job-card">
            <div class="job-header">
                <span class="job-name">{job['job_name']}</span>
                <div class="job-meta">
                    <span class="job-status {status_class}">{status_text}</span>
                    <span class="duration-badge">Duration: {duration}</span>
                </div>
            </div>

            <div class="steps-section">
                <h3>Workflow Steps</h3>
                <table class="steps-table">
                    <thead>
                        <tr>
                            <th style="width: 50%;">Step</th>
                            <th style="width: 15%;">Status</th>
                            <th style="width: 15%;">Duration</th>
                            <th style="width: 20%;">Start Time</th>
                        </tr>
                    </thead>
                    <tbody>
        '''

        for step in job.get('steps', []):
            step_status = step.get('conclusion', step.get('status', 'unknown'))
            step_duration = step.get('duration_formatted', 'N/A')
            step_started = step.get('started_at', 'N/A')
            if step_started and step_started != 'N/A':
                try:
                    dt = datetime.fromisoformat(step_started.replace('Z', '+00:00'))
                    step_started = dt.strftime('%H:%M:%S')
                except:
                    pass

            job_html += f'''
                        <tr>
                            <td class="step-name">{step.get('name', 'Unknown')}</td>
                            <td><span class="job-status status-{step_status}" style="font-size: 11px;">{step_status}</span></td>
                            <td class="step-duration">{step_duration}</td>
                            <td style="color: #8b949e; font-size: 13px;">{step_started}</td>
                        </tr>
            '''

        job_html += '''
                    </tbody>
                </table>
            </div>
        '''

        # Dockerfile Stages
        dockerfile_stages = job.get('dockerfile_stages', [])
        if dockerfile_stages:
            job_html += f'''
            <div class="stages-section">
                <h3>Dockerfile Build Stages ({len(dockerfile_stages)} stages)</h3>
            '''

            for stage in dockerfile_stages:
                stage_id = stage.get('stage_id', '#N/A')
                stage_info = stage.get('stage_info', '')
                stage_cmd = stage.get('command', '')
                stage_duration = stage.get('duration_formatted', 'N/A')
                instr_type = stage.get('instruction_type', 'OTHER')
                instr_detail = stage.get('instruction_detail', '')

                job_html += f'''
                <div class="stage-card">
                    <div class="stage-header">
                        <div>
                            <span class="stage-id">{stage_id}</span>
                            <span class="instruction-type instr-{instr_type}">{instr_type}</span>
                            <span class="stage-info">{stage_info}</span>
                        </div>
                        <span class="stage-duration">{stage_duration}</span>
                    </div>
                    <div class="stage-command">{instr_detail}</div>
                </div>
                '''

            job_html += '''
            </div>
            '''

        job_html += '''
        </div>
        '''
        jobs_html += job_html

    html = html.replace('{{JOBS_CONTENT}}', jobs_html)

    # Generate Dockerfile instruction summary HTML
    dockerfile_analysis = report['summary'].get('dockerfile_analysis', {})
    instruction_summary_html = '<div class="instruction-summary">'

    if dockerfile_analysis:
        # Sort by total duration (descending)
        sorted_types = sorted(
            dockerfile_analysis.items(),
            key=lambda x: x[1]['total_duration'],
            reverse=True
        )

        for instr_type, data in sorted_types:
            instruction_summary_html += f'''
            <div class="instr-card">
                <div class="type"><span class="instruction-type instr-{instr_type}">{instr_type}</span></div>
                <div class="count">{data['count']} stages</div>
                <div class="duration">Total: {data['total_duration_formatted']}</div>
            </div>
            '''

    instruction_summary_html += '</div>'

    # Handle empty case
    if not dockerfile_analysis:
        instruction_summary_html = '<p style="color: #8b949e;">No Dockerfile instruction data available</p>'

    html = html.replace('{{DOCKERFILE_INSTRUCTION_SUMMARY}}', instruction_summary_html)

    # Generate slowest stages HTML
    slowest = report['summary'].get('slowest_stages', [])
    slowest_html = ""
    if slowest:
        for idx, stage in enumerate(slowest, 1):
            stage_info = stage.get('stage_info', '')
            stage_cmd = stage.get('command', '')[:80]
            stage_duration = stage.get('duration_formatted', 'N/A')
            instr_type = stage.get('instruction_type', 'OTHER')

            slowest_html += f'''
            <div class="slowest-item">
                <div class="slowest-rank">{idx}</div>
                <div class="slowest-details">
                    <span class="instruction-type instr-{instr_type}">{instr_type}</span>
                    <div class="slowest-stage">{stage_info} - {stage_cmd}...</div>
                </div>
                <div class="slowest-duration">{stage_duration}</div>
            </div>
            '''
        html = html.replace('{{SLOWEST_STAGES_CONTENT}}', slowest_html)
    else:
        html = html.replace('{{SLOWEST_STAGES_CONTENT}}', '<p style="color: #8b949e;">No Dockerfile stages data available</p>')

    html_path = os.path.join(output_dir, 'build-report.html')
    with open(html_path, 'w') as f:
        f.write(html)

    print(f"HTML report generated: {html_path}")
    return html_path


def main():
    """Main entry point."""
    gh_token = os.environ.get('GH_TOKEN')
    run_id = os.environ.get('RUN_ID')
    repo = os.environ.get('REPO')
    output_dir = os.environ.get('OUTPUT_DIR', '.')

    if not all([gh_token, run_id, repo]):
        print("Error: Missing required environment variables")
        print("Required: GH_TOKEN, RUN_ID, REPO")
        print("Optional: OUTPUT_DIR (default: '.')")
        exit(1)

    print(f"Generating build report for run {run_id} in {repo}...")

    # Generate JSON report
    report = generate_build_report(gh_token, run_id, repo, output_dir)

    # Generate HTML report
    generate_html_report(report, output_dir)

    print("Build report generation complete!")


if __name__ == '__main__':
    main()
