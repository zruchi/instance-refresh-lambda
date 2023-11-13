#!/usr/bin/env python3
# vim:syntax=python ts=4 sw=4 et

#
# 1. get the new newest version of the image
# 2. create a new launch_template_version using the new image metadata
# 3. trigger an instance-refresh with the new template version
#
import boto3
import botocore.exceptions
import logging
import os
import sys


def get_latest_image(ec2):
    images = ec2.describe_images(
        Filters=
        [
            {'Name': 'name', 'Values': ['ubuntu20.04-zendesk-base-*']},
            {'Name': 'state', 'Values': ['available']},
            {'Name': 'tag:Hostgroup', 'Values': ['base']},
        ],
        Owners=['self']
    )
    sorted_ami = sorted(images['Images'], key=lambda x: x['CreationDate'], reverse=True)
    latest_ami = sorted_ami[0]['ImageId']

    return latest_ami


def launch_template_versions(ec2, image_id):
    output = {}
    try:
        template = ec2.describe_launch_template_versions(
            LaunchTemplateName='network-nat-instance-lt',
        )
    except botocore.exceptions.ClientError as e:
        raise e

    template_id = template['LaunchTemplateVersions'][0]['LaunchTemplateId']
    template_version = template['LaunchTemplateVersions'][0]['VersionNumber']
    template_image = template['LaunchTemplateVersions'][0]['LaunchTemplateData']['ImageId']

    if template_image != image_id:
        try:
            response = ec2.create_launch_template_version(
                LaunchTemplateId=template_id,
                SourceVersion=template_version,
                LaunchTemplateData={
                    'ImageId': image_id,
                }
            )
        except botocore.exceptions.ClientError as e:
            raise e

        # now if that is a success set the new version as the default
        default_version = response['LaunchTemplateVersion']['VersionNumber']
        logging.info('Setting the %s as the default template version.', default_version)
        ec2.modify_launch_template(
            LaunchTemplateId=template_id,
            DefaultVersion=default_version
        )

        # we can't keep increasing the template version
        # that is cumbersome to manage a huge number of template version
        # we should keep the number manageable maybe 10 version?
        versions = template['LaunchTemplateVersions']
        while len(versions) > 10:
            logging.info('Deleting version: %', versions[-1]['VersionNumber'])
            ec2.delete_launch_template_versions(
                LaunchTemplateId=template_id,
                Versions=[versions[-1]['VersionNumber']]
            )
    else:
        logging.info('The image is still current - current template version is: %s', template_version)
        return

    output['current_image_id'] = template_image
    output['template_id'] = template_id
    output['template_version'] = template_version
    output['new_template_version'] = response['LaunchTemplateVersion']['VersionNumber']

    return output


def trigger_refresh(asg, template):
    try:
        scaling_groups = asg.describe_auto_scaling_groups(
            Filters=
            [
                {'Name': 'tag:Hostgroup', 'Values': ['nat_instance']},
                {'Name': 'tag:team', 'Values': ['network']},
                {'Name': 'tag:product', 'Values': ['foundation']},
            ]
        )
    except botocore.exceptions.ClientError as e:
        raise e

    version = template['new_template_version']
    for group in scaling_groups['AutoScalingGroups']:
        group_name = group['AutoScalingGroupName']
        logging.info('Rotating the auto-scaling group %s with template version %s', (group_name, version))
        try:
            asg.start_instance_refresh(
                AutoScalingGroupName=group_name,
                Strategy='Rolling',
                DesiredConfiguration={
                    'LaunchTemplate': {
                        'LaunchTemplateId': template['template_id'],
                        'Version': version
                    }
                }
            )
        except botocore.exceptions.ClientError as e:
            raise e


def main():
    # setup logging
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)

    runtime = os.getenv('AWS_EXECUTION_ENV')
    if runtime is not None:
        session = boto3.session.Session()
    else:
        session = boto3.session.Session(profile_name='sandbox1')

    region = os.getenv('AWS_REGION')
    if region is None:
        logging.info('No region in ENV vars - using the default session region: %s', session.region_name)
        region = session.region_name

    client = session.client('ec2', region)

    image_id = get_latest_image(client)
    logging.info('Newest ami is: %s', image_id)

    template = launch_template_versions(client, image_id)

    if template is not None:
        asg = session.client('autoscaling', region)
        trigger_refresh(asg, template)


if __name__ == '__main__':
    main()
