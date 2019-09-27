"""Test changing provider versions."""
from runway.util import change_dir
from test_terraform.test_terraform import Terraform
from util import run_command


class ProviderTest(Terraform):
    """Test changing between provider versions."""

    TEST_NAME = __name__
    
    def __init__(self, logger):
        """Init class."""
        self.logger = logger

    def deploy_provider(self, version):
        """Deploy provider."""
        self.copy_template('provider-version{}.tf'.format(version))
        self.copy_runway('s3')
        with change_dir(self.base_dir):
            return run_command(['runway', 'deploy'])

    def run(self):
        """Run tests."""
        self.clean()
        self.run_stacker()
        self.set_tf_version(11)

        assert self.deploy_provider(1) == 0, '{}: Provider version 1 failed'.format(__name__)
        assert self.deploy_provider(2) == 0, '{}: Provider version 2 failed'.format(__name__)

    def teardown(self):
        """Teardown any created resources."""
        self.logger.info('Tearing down: %s', self.TEST_NAME)
        with change_dir(self.base_dir):
            run_command(['runway', 'destroy'])
        self.clean()