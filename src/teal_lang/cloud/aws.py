"""Manage Teal deployments in AWS"""

import base64
import zipfile
import functools
import json
import logging
import os
import os.path
import random
import shutil
import subprocess
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Tuple
from zipfile import ZipFile

import boto3
import botocore
from botocore.client import ClientError

import deterministic_zip as dz

from .. import config as teal_config

LOG = logging.getLogger(__name__)


# https://medium.com/uk-hydrographic-office/developing-and-testing-lambdas-with-pytest-and-localstack-21a111b7f6e8

THIS_DIR = Path(__file__).parent


class DeploymentFailed(Exception):
    """Failed to deploy"""


class InvokeError(Exception):
    "Failed to invoke function"


@functools.lru_cache
def get_client(config, service):
    """Get a boto3 client for SERVICE, setting endpoint and region"""
    args = {}

    endpoint_url = os.environ.get("AWS_ENDPOINT", None)
    if endpoint_url:
        args["endpoint_url"] = endpoint_url

    return boto3.client(
        service,
        region_name=config.service.region,
        # config=botocore.config.Config(retries={"max_attempts": 0}),
        **args,
    )


def hash_file(filename: Path) -> str:
    """Get the (base64 encoded) SHA256 hash of a file"""
    with open(filename, "rb") as f:
        return base64.b64encode(sha256(f.read()).digest()).decode()


# Client: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#client
class DataBucket:
    @staticmethod
    def resource_name(config):
        return f"teal-{config.service.deployment_id}-{config.service.name}"

    @staticmethod
    def exists(config):
        client = get_client(config, "s3")
        name = DataBucket.resource_name(config)
        try:
            client.head_bucket(Bucket=name)
            return True
        except ClientError:
            return False

    @staticmethod
    def create(config):
        name = DataBucket.resource_name(config)
        client = get_client(config, "s3")

        # Example: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-example-creating-buckets.html
        try:
            location = {"LocationConstraint": config.service.region}
            client.create_bucket(
                Bucket=name, ACL="private", CreateBucketConfiguration=location
            )
            LOG.info(f"created bucket {name}")
        except ClientError as exc:
            raise DeploymentFailed from exc

    @staticmethod
    def create_or_update(config):
        """Create the data bucket if it doesn't exist"""
        name = DataBucket.resource_name(config)
        client = get_client(config, "s3")
        if not DataBucket.exists(config):
            DataBucket.create(config)

    @staticmethod
    def delete_if_exists(config):
        name = DataBucket.resource_name(config)
        client = get_client(config, "s3")
        try:
            client.delete_bucket(Bucket=name)
            LOG.info(f"deleted bucket {name}")
        except client.exceptions.NoSuchBucket:
            pass


def get_data_dir(config) -> Path:
    """Get path to the Teal data directory, ensuring it exists"""
    data_dir = Path(config.service.data_dir)

    if not data_dir.is_absolute():
        data_dir = (Path(config.root) / config.service.data_dir).resolve()

    if not data_dir.is_dir():
        os.makedirs(str(data_dir))

    return data_dir


def upload_if_necessary(client, bucket, key, filename):
    """Upload a file if it doesn't already exist"""
    new_hashsum = hash_file(filename)
    hash_key = "sha256"

    # only upload if the object doesn't exist or the hash is different
    try:
        res = client.head_object(Bucket=bucket, Key=key)
        if new_hashsum == res["Metadata"][hash_key]:
            return
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Message"] == "Not Found":
            pass
    except KeyError:
        # also catch KeyError just in case
        pass

    try:
        with open(filename, "rb") as f:
            response = client.put_object(
                Body=f, Bucket=bucket, Key=key, Metadata={hash_key: new_hashsum}
            )
            LOG.info(f"Uploaded {filename}")

    except ClientError as exc:
        raise DeploymentFailed from exc


class S3File:
    """A file in S3. This class must be subclassed"""

    @classmethod
    def resource_name(cls, config) -> list:
        # NOTE: no nested folders. Could do later if necessary.
        return cls.key

    @classmethod
    def local_file(cls, config) -> Path:
        data_dir = get_data_dir(config)
        return data_dir / cls.key

    @classmethod
    def local_sha(cls, config) -> str:
        local_file = cls.local_file(config)
        return hash_file(local_file)

    @classmethod
    def create_or_update(cls, config):
        """Create the file and upload it"""
        dest_file = cls.local_file(config)
        cls.get_file(config, dest_file)

        client = get_client(config, "s3")
        bucket = DataBucket.resource_name(config)
        upload_if_necessary(client, bucket, cls.key, str(dest_file))

    @classmethod
    def delete_if_exists(cls, config):
        client = get_client(config, "s3")
        bucket = DataBucket.resource_name(config)
        try:
            client.delete_object(Bucket=bucket, Key=cls.key)
            LOG.info(f"{cls.key} deleted from {bucket}")
        except (client.exceptions.NoSuchKey, client.exceptions.NoSuchBucket):
            pass


class TealPackage(S3File):
    key = "teal.zip"

    @staticmethod
    def get_file(config, dest: Path):
        """Create the Teal code Zip, saving it in dest"""
        root = Path(__file__).parents[3]

        LOG.info(f"Copying Teal Lambda package to {dest}")
        shutil.copyfile(root / "teal_lambda.zip", dest)


def zip_dir(dirname: Path, dest: Path, deterministic=True):
    """Zip a directory"""
    # https://github.com/bboe/deterministic_zip/blob/master/deterministic_zip/__init__.py
    with zipfile.ZipFile(dest, "w") as zip_file:
        dz.add_directory(zip_file, dirname, os.path.basename(dirname))


class SourceLayerPackage(S3File):
    key = "layer.zip"

    @staticmethod
    def get_file(config, dest: Path):
        """Create the source code layer Zip, saving it in dest"""
        root = Path(__file__).parents[3]

        LOG.info(f"Building Source Layer package in {dest}...")
        workdir = get_data_dir(config) / "source_build" / "python"
        os.makedirs(str(workdir), exist_ok=True)
        shutil.copytree(
            config.service.python_src,
            workdir / config.service.python_src.name,
            dirs_exist_ok=True,
        )
        reqs_file = config.service.python_requirements
        subprocess.check_output(
            f'pip install -q --target "{workdir}" -r {reqs_file}'.split(" ")
        )
        zip_dir(workdir, dest)


# Client: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html#client
class DataTable:
    @staticmethod
    def resource_name(config):
        return f"teal-{config.service.deployment_id}-{config.service.name}"

    @staticmethod
    def exists(config):
        client = get_client(config, "dynamodb")
        name = DataTable.resource_name(config)
        try:
            client.describe_table(TableName=name)
            return True
        except client.exceptions.ResourceNotFoundException:
            return False

    @staticmethod
    def get_arn(config):
        client = get_client(config, "dynamodb")
        name = DataTable.resource_name(config)
        res = client.describe_table(TableName=name)
        return res["Table"]["TableArn"]

    @staticmethod
    def create_or_update(config):
        client = get_client(config, "dynamodb")
        name = DataTable.resource_name(config)

        if DataTable.exists(config):
            return

        client.create_table(
            TableName=name,
            AttributeDefinitions=[
                # --
                dict(AttributeName="session_id", AttributeType="S")
            ],
            KeySchema=[
                # --
                dict(AttributeName="session_id", KeyType="HASH")
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        waiter = client.get_waiter("table_exists")
        waiter.wait(TableName=name)
        LOG.info(f"Table {name} created")

    @staticmethod
    def delete_if_exists(config):
        client = get_client(config, "dynamodb")
        name = DataTable.resource_name(config)
        try:
            client.delete_table(TableName=name)
            waiter = client.get_waiter("table_not_exists")
            waiter.wait(TableName=name)
            LOG.info(f"Table {name} deleted")
        except client.exceptions.ResourceNotFoundException:
            pass


# Client: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/iam.html#client
class ExecutionRole:
    @staticmethod
    def resource_name(config):
        return f"teal-{config.service.deployment_id}-{config.service.name}"

    @staticmethod
    def get_arn(config):
        client = get_client(config, "iam")
        res = client.get_role(RoleName=ExecutionRole.resource_name(config))
        return res["Role"]["Arn"]

    @staticmethod
    def exists(config):
        client = get_client(config, "iam")
        try:
            res = client.get_role(RoleName=ExecutionRole.resource_name(config))
            return True
        except client.exceptions.NoSuchEntityException:
            return False

    @staticmethod
    def delete_if_exists(config):
        if not ExecutionRole.exists(config):
            return

        client = get_client(config, "iam")
        name = ExecutionRole.resource_name(config)

        try:
            client.delete_role_policy(RoleName=name, PolicyName="default")
        except client.exceptions.NoSuchEntityException:
            pass

        try:
            client.detach_role_policy(
                RoleName=name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            )
        except client.exceptions.NoSuchEntityException:
            pass

        client.delete_role(RoleName=name)

    @staticmethod
    def create_or_update(config):
        if ExecutionRole.exists(config):
            return

        client = get_client(config, "iam")
        name = ExecutionRole.resource_name(config)
        table_arn = DataTable.get_arn(config)

        policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    # TODO log streams
                    # {
                    #     "Action": ["logs:CreateLogStream", "logs:CreateLogGroup"],
                    #     "Resource": [
                    #         "arn:aws:logs:eu-west-2:297409317403:log-group:/aws/lambda/tryit-prod*:*"
                    #     ],
                    #     "Effect": "Allow",
                    # },
                    # {
                    #     "Action": ["logs:PutLogEvents"],
                    #     "Resource": [
                    #         "arn:aws:logs:eu-west-2:297409317403:log-group:/aws/lambda/tryit-prod*:*:*"
                    #     ],
                    #     "Effect": "Allow",
                    # },
                    {
                        "Action": [
                            "dynamodb:Query",
                            "dynamodb:Scan",
                            "dynamodb:GetItem",
                            "dynamodb:PutItem",
                            "dynamodb:UpdateItem",
                            "dynamodb:DeleteItem",
                            "dynamodb:DescribeTable",
                        ],
                        "Resource": table_arn,
                        "Effect": "Allow",
                    },
                    {
                        "Action": ["lambda:InvokeFunction"],
                        "Resource": "*",  # TODO make it only the resume FN?
                        "Effect": "Allow",
                    },
                ],
            }
        )

        assume_role_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )

        client.create_role(
            RoleName=name, AssumeRolePolicyDocument=assume_role_policy,
        )
        client.put_role_policy(
            RoleName=name, PolicyName="default", PolicyDocument=policy
        )
        client.attach_role_policy(
            RoleName=name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        # There isn't a waiter to check that it's propagated :(
        # See also https://github.com/Miserlou/Zappa/commit/fa1b224fc43c7c8739dd179f9a038d31e13911e9
        # Hack for now:
        time.sleep(10)


class SourceLayer:
    @staticmethod
    def resource_name(config):
        return f"teal-{config.service.deployment_id}-{config.service.name}-src"

    @staticmethod
    def get_arn(config):
        """Get ARN and SHA256 of the highest-version layer"""
        client = get_client(config, "lambda")
        name = SourceLayer.resource_name(config)
        res = client.list_layer_versions(LayerName=name, MaxItems=1)
        try:
            return res["LayerVersions"][0]["LayerVersionArn"]
        except IndexError:
            return None

    @staticmethod
    def get_latest_sha256(config):
        """Get ARN and SHA256 of the highest-version layer"""
        client = get_client(config, "lambda")
        arn = SourceLayer.get_arn(config)
        res = client.get_layer_version_by_arn(Arn=arn)
        return res["Content"]["CodeSha256"]

    @staticmethod
    def create_or_update(config):
        current_sha = SourceLayer.get_latest_sha256(config)
        local_sha = SourceLayerPackage.local_sha(config)

        if current_sha == local_sha:
            return

        LOG.info(f"Layer hash changed, updating ({current_sha}, {local_sha})")
        client = get_client(config, "lambda")
        name = SourceLayer.resource_name(config)
        client.publish_layer_version(
            LayerName=name,
            Content=dict(
                # --
                S3Bucket=DataBucket.resource_name(config),
                S3Key=SourceLayerPackage.key,
            ),
        )
        current_sha = SourceLayer.get_latest_sha256(config)
        assert current_sha == local_sha

    @staticmethod
    def delete_if_exists(config):
        pass  # FIXME


# Client: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/lambda.html#client
class TealFunction:
    needs_src = False

    @classmethod
    def resource_name(cls, config):
        return f"teal-{config.service.deployment_id}-{config.service.name}-{cls.name}"

    @classmethod
    def exists(cls, config):
        client = get_client(config, "lambda")
        name = cls.resource_name(config)
        try:
            res = client.get_function(FunctionName=name)
            return True
        except client.exceptions.ResourceNotFoundException:
            return False

    @classmethod
    def create_or_update(cls, config):
        if cls.exists(config):
            needs_publish = cls.update(config)
        else:
            cls.create(config)
            needs_publish = True

        if needs_publish:
            client = get_client(config, "lambda")
            name = cls.resource_name(config)
            client.publish_version(FunctionName=name)

            waiter = client.get_waiter("function_active")
            waiter.wait(FunctionName=name)

    @classmethod
    def update(cls, config) -> bool:
        client = get_client(config, "lambda")
        name = cls.resource_name(config)
        needs_publish = False

        current_config = client.get_function_configuration(FunctionName=name)
        current_sha = current_config["CodeSha256"]

        if current_sha != TealPackage.local_sha(config):
            LOG.info(f"Code hash for {name} changed, updating function")
            client.update_function_code(
                FunctionName=name,
                S3Bucket=DataBucket.resource_name(config),
                S3Key=TealPackage.key,
                Publish=False,
            )
            needs_publish = True

        latest_layer = SourceLayer.get_arn(config)
        if cls.needs_src and current_config["Layers"][0]["Arn"] != latest_layer:
            LOG.info(f"Layer ARN for {name} changed, updating function")
            client.update_function_configuration(
                FunctionName=name, Layers=[SourceLayer.get_arn(config)]
            )
            needs_publish = True

        return needs_publish

    @classmethod
    def create(cls, config):
        client = get_client(config, "lambda")
        role_arn = ExecutionRole.get_arn(config)
        name = cls.resource_name(config)
        layers = [SourceLayer.get_arn(config)] if cls.needs_src else []

        client.create_function(
            FunctionName=name,
            Runtime="python3.8",
            Role=role_arn,
            Handler=cls.handler,
            Publish=False,
            Code=dict(
                # --
                S3Bucket=DataBucket.resource_name(config),
                S3Key=TealPackage.key,
            ),
            Timeout=config.service.lambda_timeout,  # TODO make per-function?
            Layers=layers,
            Environment=dict(
                Variables={
                    "TL_REGION": config.service.region,
                    "DYNAMODB_TABLE": DataTable.resource_name(config),
                    "USE_LIVE_AWS": "foo",  # setting this to "yes" breaks AWS...?
                    "RESUME_FN_NAME": FnResume.resource_name(config),
                }
            ),
        )

    @classmethod
    def delete_if_exists(cls, config):
        client = get_client(config, "lambda")
        name = cls.resource_name(config)
        try:
            client.delete_function(FunctionName=name)
        except client.exceptions.ResourceNotFoundException:
            pass

    @classmethod
    def invoke(cls, config, data: dict) -> Tuple[str, str]:
        client = get_client(config, "lambda")
        name = cls.resource_name(config)

        payload = bytes(json.dumps(data), "utf-8")

        if not payload:
            payload = bytes("", "utf-8")

        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/lambda.html#Lambda.Client.invoke
        response = client.invoke(
            FunctionName=name,
            InvocationType="RequestResponse",
            Payload=payload,
            LogType="Tail",
        )

        if not 200 <= response["StatusCode"] < 300:
            raise InvokeError(response)

        logs = base64.b64decode(response["LogResult"]).decode()
        payload = response["Payload"].read().decode("utf-8")

        return logs, payload


class FnSetexe(TealFunction):
    name = "set_exe"
    handler = "teal_lang.executors.awslambda.set_exe"


class FnNew(TealFunction):
    name = "new"
    handler = "teal_lang.executors.awslambda.new"
    needs_src = True


class FnResume(TealFunction):
    name = "resume"
    handler = "teal_lang.executors.awslambda.resume"
    needs_src = True


class FnGetOutput(TealFunction):
    name = "getoutput"
    handler = "teal_lang.executors.awslambda.getoutput"


class FnGetEvents(TealFunction):
    name = "getevents"
    handler = "teal_lang.executors.awslambda.getevents"


class FnVersion(TealFunction):
    name = "version"
    handler = "teal_lang.executors.awslambda.version"


# ... TODO


CORE_RESOURCES = [
    DataBucket,
    TealPackage,
    SourceLayerPackage,
    DataTable,
    ExecutionRole,
    SourceLayer,
    FnSetexe,
    FnNew,
    FnResume,
    FnGetOutput,
    FnGetEvents,
    FnVersion,
]


def deploy(config):
    """Deploy (or update) infrastructure for this config"""
    LOG.info(f"Deploying: {config.service.deployment_id}")
    start = time.time()

    # TODO parallelise some deployment for funs.
    for res in CORE_RESOURCES:
        res.create_or_update(config)

    end = time.time()
    print(f"Deployed ({int(end-start)}s elapsed).")


def destroy(config):
    """Destroy infrastructure created for this config"""
    LOG.info(f"Destroying: {config.service.deployment_id}")

    # destroy in reverse order so dependencies go first
    for res in reversed(CORE_RESOURCES):
        res.delete_if_exists(config)


def show(config):
    """Show infrastructure state"""
    for res in CORE_RESOURCES:
        # TODO only show if deployed
        print(f"- {res.__name__}: {res.resource_name(config)}")


# This is a bit pointless for now - could be a generic cloud interface in the
# future.
@dataclass
class Interface:
    set_exe: type
    new: type
    get_output: type
    get_events: type
    version: type


def get_api() -> Interface:
    # TODO check it's deployed?
    return Interface(FnSetexe, FnNew, FnGetOutput, FnGetEvents, FnVersion)