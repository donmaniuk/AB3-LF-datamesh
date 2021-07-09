from constants import *
import json
import os
import pystache
import time
import boto3


def validate_correct_account(iam_client, role_must_exist: str):
    try:
        iam_client.get_role(RoleName=role_must_exist)
        return True
    except iam_client.exceptions.NoSuchEntityException:
        return False


def generate_policy(template_file: str, config: dict):
    with open("%s/%s" % (os.path.join(os.path.dirname(__file__), "../resource"), template_file)) as t:
        template = t.read()

    rendered = pystache.Renderer().render(template, config)

    return rendered


def add_aws_trust_to_role(iam_client, account_id: str, role_name: str):
    '''
    Private method to add a trust relationship to an AWS Account to a Role
    :return:
    '''
    # validate that the account is suitable for configuration due to it having the DataMeshManager role installed
    validate_correct_account(iam_client, role_name)

    # update the  trust policy to include the provided account ID
    response = iam_client.get_role(RoleName=role_name)

    policy_doc = response.get('Role').get('AssumeRolePolicyDocument')

    # add the account to the trust relationship
    trusted_entities = policy_doc.get('Statement')[0].get('Principal').get('AWS')
    if account_id not in trusted_entities:
        trusted_entities.append(account_id)
        policy_doc.get('Statement')[0].get('Principal')['AWS'] = trusted_entities

    iam_client.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(policy_doc))


def create_assume_role_doc(aws_principals: list = None, resource: str = None, additional_principals: dict = None):
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
            }
        ]
    }

    # add the mandatory AWS principals
    if aws_principals is not None:
        document.get('Statement')[0]['Principal'] = {"AWS": aws_principals}

    # add the additional map of principals provided
    if additional_principals is not None:
        for k, v in additional_principals.items():
            document.get('Statement')[0]['Principal'][k] = v

    if resource is not None:
        document.get('Statement')[0]['Resource'] = resource

    return document


def configure_iam(iam_client, policy_name: str, policy_desc: str, policy_template: str,
                  role_name: str, role_desc: str, account_id: str, config: dict = None,
                  additional_assuming_principals: dict = None):
    policy_arn = None
    try:
        # create an IAM Policy from the template
        policy_doc = generate_policy(policy_template, config)

        response = iam_client.create_policy(
            PolicyName=policy_name,
            Path=DATA_MESH_IAM_PATH,
            PolicyDocument=policy_doc,
            Description=policy_desc,
            Tags=DEFAULT_TAGS
        )
        policy_arn = response.get('Policy').get('Arn')
    except iam_client.exceptions.EntityAlreadyExistsException:
        policy_arn = "arn:aws:iam::%s:policy%s%s" % (account_id, DATA_MESH_IAM_PATH, policy_name)

    # create a non-root user who can assume the role
    try:
        response = iam_client.create_user(
            Path=DATA_MESH_IAM_PATH,
            UserName=role_name,
            Tags=DEFAULT_TAGS
        )

        # have to sleep for a second here, as there appears to be eventual consistency between create_user and create_role
        time.sleep(.5)
    except iam_client.exceptions.EntityAlreadyExistsException:
        pass

    user_arn = "arn:aws:iam::%s:user%s%s" % (account_id, DATA_MESH_IAM_PATH, role_name)

    # create a group for the user
    try:
        response = iam_client.create_group(
            Path=DATA_MESH_IAM_PATH,
            GroupName=("%sGroup" % role_name)
        )
    except iam_client.exceptions.EntityAlreadyExistsException:
        pass

    group_arn = "arn:aws:iam::%s:group%s%sGroup" % (account_id, DATA_MESH_IAM_PATH, role_name)

    # put the user into the group
    try:
        response = iam_client.add_user_to_group(
            GroupName=("%sGroup" % role_name),
            UserName=role_name
        )
    except iam_client.exceptions.EntityAlreadyExistsException:
        pass

    role_arn = None
    try:
        # now create the IAM Role with a trust policy to the indicated principal and the root user
        aws_principals = [user_arn, ("arn:aws:iam::%s:root" % account_id)]

        role_response = iam_client.create_role(
            Path=DATA_MESH_IAM_PATH,
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(
                create_assume_role_doc(aws_principals=aws_principals,
                                       additional_principals=additional_assuming_principals)),
            Description=role_desc,
            Tags=DEFAULT_TAGS
        )

        role_arn = role_response.get('Role').get('Arn')
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = iam_client.get_role(RoleName=role_name).get(
            'Role').get('Arn')

    # attach the created policy to the role
    iam_client.attach_role_policy(
        RoleName=role_name,
        PolicyArn=policy_arn
    )

    create_assume_role_policy(iam_client, account_id, ("Assume%s" % role_name), role_arn)

    # now let the group assume the role
    iam_client.attach_group_policy(GroupName=("%sGroup" % role_name), PolicyArn=policy_arn)

    # TODO Grant permissions for IamAllowedPrincipals to SUPER for this Account
    return role_arn


def flatten_default_tags():
    output = {}
    for t in DEFAULT_TAGS:
        output[t.get('Key')] = t.get('Value')

    return output


def create_assume_role_policy(iam_client, account_id, policy_name, role_arn):
    # create a policy that lets someone assume this new role
    policy_arn = None
    try:
        response = iam_client.create_policy(
            PolicyName=policy_name,
            Path=DATA_MESH_IAM_PATH,
            PolicyDocument=json.dumps(create_assume_role_doc(resource=role_arn)),
            Description=("Policy allowing the grantee the ability to assume Role %s" % role_arn),
            Tags=DEFAULT_TAGS
        )
        policy_arn = response.get('Policy').get('Arn')
    except iam_client.exceptions.EntityAlreadyExistsException:
        policy_arn = "arn:aws:iam::%s:policy%s%s" % (account_id, DATA_MESH_IAM_PATH, policy_name)

    return policy_arn


def generate_client(service: str, region: str, credentials: dict):
    return boto3.client(service_name=service, region_name=region, aws_access_key_id=credentials.get('AccessKeyId'),
                        aws_secret_access_key=credentials.get('SecretAccessKey'),
                        aws_session_token=credentials.get('SessionToken'))
