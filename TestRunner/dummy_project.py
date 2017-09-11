# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper class for dummy Xcode project generated by prebuilt bundles.

The dummy project supports sdk iphonesimulator and test type XCUITest. It can
be run with `xcodebuild build-for-testing`.
See how to create it in //devtools/forge/mac/testrunner/TestProject/README.
"""

import logging
import os
import pkgutil
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from XCTestRunner.Shared import bundle_util
from XCTestRunner.Shared import ios_constants
from XCTestRunner.Shared import ios_errors
from XCTestRunner.Shared import plist_util
from XCTestRunner.Shared import provisioning_profile
from XCTestRunner.Shared import xcode_info_util
from XCTestRunner.TestRunner import xcodebuild_test_executor


_DEFAULT_PERMS = 0777
_DUMMYPROJECT_DIR_NAME = 'TestProject'
_DUMMYPROJECT_XCODEPROJ_NAME = 'TestProject.xcodeproj'
_DUMMYPROJECT_PBXPROJ_NAME = 'project.pbxproj'
_DUMMYPROJECT_XCTESTS_SCHEME = 'TestProjectXctest'
_DUMMYPROJECT_XCUITESTS_SCHEME = 'TestProjectXcuitest'

_SIGNAL_BUILD_FOR_TESTING_SUCCEED = '** TEST BUILD SUCCEEDED **'


class DummyProject(object):
  """Handles a dummy project with prebuilt bundles."""

  def __init__(self, app_under_test_dir, test_bundle_dir,
               sdk=ios_constants.SDK.IPHONESIMULATOR,
               test_type=ios_constants.TestType.XCUITEST,
               work_dir=None):
    """Initializes the DummyProject object.

    Args:
      app_under_test_dir: string, path of the app to be tested in
          dummy project.
      test_bundle_dir: string, path of the test bundle.
      sdk: string, SDKRoot of the dummy project. See supported SDKs in
          module Shared.ios_constants.
      test_type: string, test type of the test bundle. See supported test types
          in module Shared.ios_constants.
      work_dir: string, work directory which contains run files.
    """
    self._app_under_test_dir = app_under_test_dir
    self._test_bundle_dir = test_bundle_dir
    self._sdk = sdk
    self._test_type = test_type
    if work_dir:
      self._work_dir = os.path.join(work_dir, 'dummy_project')
    else:
      self._work_dir = None
    self._dummy_project_path = None
    self._xcodeproj_dir_path = None
    self._pbxproj_file_path = None
    self._is_dummy_project_generated = False
    self._delete_work_dir = False
    self._ValidateArguments()
    self._test_scheme = None
    if test_type == ios_constants.TestType.XCTEST:
      self._test_scheme = _DUMMYPROJECT_XCTESTS_SCHEME
    elif test_type == ios_constants.TestType.XCUITEST:
      self._test_scheme = _DUMMYPROJECT_XCUITESTS_SCHEME

  def __enter__(self):
    self.GenerateDummyProject()
    return self

  def __exit__(self, unused_type, unused_value, unused_traceback):
    """Deletes the temp directories."""
    self.Close()

  @property
  def pbxproj_file_path(self):
    """Gets the pbxproj file path of the dummy project."""
    return self._pbxproj_file_path

  @property
  def test_scheme_path(self):
    """Gets the test scheme path of the dummy project."""
    return os.path.join(
        self._xcodeproj_dir_path,
        'xcshareddata/xcschemes',
        '%s.xcscheme' % self._test_scheme)

  def BuildForTesting(self, built_products_dir, derived_data_dir):
    """Runs `xcodebuild build-for-testing` with the dummy project.

    If app under test or test bundle are not in built_products_dir, will copy
    the file into built_products_dir.

    Args:
      built_products_dir: path of the built products dir in this build session.
      derived_data_dir: path of the derived data dir in this build session.
    Raises:
      BuildFailureError: when failed to build the dummy project.
    """
    self.GenerateDummyProject()
    self._PrepareBuildProductsDir(built_products_dir)
    logging.info('Running `xcodebuild build-for-testing` with dummy project.\n'
                 'built_product_dir = %s\nderived_data_path = %s\n',
                 built_products_dir,
                 derived_data_dir)
    command = ['xcodebuild', 'build-for-testing',
               'BUILT_PRODUCTS_DIR=' + built_products_dir,
               'SDKROOT=' + self._sdk,
               '-project', self._xcodeproj_dir_path,
               '-scheme', self._test_scheme,
               '-derivedDataPath', derived_data_dir]
    run_env = dict(os.environ)
    run_env['NSUnbufferedIO'] = 'YES'
    output = subprocess.check_output(
        command, env=run_env, stderr=subprocess.STDOUT)
    if _SIGNAL_BUILD_FOR_TESTING_SUCCEED not in output:
      raise ios_errors.BuildFailureError('Failed to build the dummy project. '
                                         'Output is:\n%s' % output)

  def RunXcTest(self, device_id, built_products_dir, derived_data_dir):
    """Runs `xcodebuild test` with the dummy project.

    If app under test or test bundle are not in built_products_dir, will copy
    the file into built_products_dir.

    Args:
      device_id: string, id of the device.
      built_products_dir: path of the built products dir in this build session.
      derived_data_dir: path of the derived data dir in this build session.

    Raises:
      IllegalArgumentError: when test type is not xctest.
    """
    if self._test_type != ios_constants.TestType.XCTEST:
      raise ios_errors.IllegalArgumentError(
          'Only xctest dummy project is supported to run `xcodebuild test`. '
          'The test type %s is not supported.' % self._test_type)
    self.GenerateDummyProject()
    # In Xcode 7.3+, the folder structure of app under test is changed.
    if xcode_info_util.GetXcodeVersionNumber() >= 730:
      app_under_test_plugin_path = os.path.join(self._app_under_test_dir,
                                                'PlugIns')
      if not os.path.exists(app_under_test_plugin_path):
        os.mkdir(app_under_test_plugin_path)
      test_bundle_under_plugin_path = os.path.join(
          app_under_test_plugin_path, os.path.basename(self._test_bundle_dir))
      if not os.path.exists(test_bundle_under_plugin_path):
        shutil.copytree(self._test_bundle_dir, test_bundle_under_plugin_path)
    self._PrepareBuildProductsDir(built_products_dir)

    logging.info('Running `xcodebuild test` with dummy project.\n'
                 'device_id= %s\n'
                 'built_product_dir = %s\nderived_data_path = %s\n',
                 device_id,
                 built_products_dir,
                 derived_data_dir)
    command = ['xcodebuild', 'test',
               'BUILT_PRODUCTS_DIR=' + built_products_dir,
               '-project', self._xcodeproj_dir_path,
               '-scheme', self._test_scheme,
               '-destination', 'id=' + device_id,
               '-derivedDataPath', derived_data_dir]
    xcodebuild_test_executor.XcodebuildTestExecutor(
        command, sdk=self._sdk, test_type=self._test_type).Execute()

  def GenerateDummyProject(self):
    """Generates the dummy project according to the specification.

    Raises:
      IllegalArgumentError: when the sdk or test type is not supported.
    """
    if self._is_dummy_project_generated:
      return
    logging.info('Generating dummy project.')

    if self._work_dir:
      if not os.path.exists(self._work_dir):
        os.mkdir(self._work_dir)
    else:
      self._work_dir = tempfile.mkdtemp()
      self._delete_work_dir = True
    self._dummy_project_path = os.path.join(self._work_dir,
                                            _DUMMYPROJECT_DIR_NAME)
    shutil.copytree(_GetTestProject(self._work_dir), self._dummy_project_path)
    for root, dirs, files in os.walk(self._dummy_project_path):
      for d in dirs:
        os.chmod(os.path.join(root, d), _DEFAULT_PERMS)
      for f in files:
        os.chmod(os.path.join(root, f), _DEFAULT_PERMS)
    self._xcodeproj_dir_path = os.path.join(
        self._dummy_project_path, _DUMMYPROJECT_XCODEPROJ_NAME)
    self._pbxproj_file_path = os.path.join(
        self._xcodeproj_dir_path, _DUMMYPROJECT_PBXPROJ_NAME)

    # Set the iOS deployment target in pbxproj.
    # If don't set this field, the default value will be the latest supported
    # iOS version which may make the app installation failure.
    self._SetIosDeploymentTarget()

    # Overwrite the pbxproj file content for test type specific.
    if self._test_type == ios_constants.TestType.XCUITEST:
      self._SetPbxprojForXcuitest()
    elif self._test_type == ios_constants.TestType.XCTEST:
      self._SetPbxprojForXctest()

    self._is_dummy_project_generated = True
    logging.info('Dummy project is generated.')

  def Close(self):
    """Deletes the temp directories."""
    if self._delete_work_dir and os.path.exists(self._work_dir):
      shutil.rmtree(self._work_dir)

  def _ValidateArguments(self):
    """Checks whether the arguments of the dummy project is valid.

    Raises:
      IllegalArgumentError: when the sdk or test type is not supported.
    """
    if self._sdk not in ios_constants.SUPPORTED_SDKS:
      raise ios_errors.IllegalArgumentError(
          'The sdk %s is not supported. Supported sdks are %s.'
          % (self._sdk, ios_constants.SUPPORTED_SDKS))
    if self._test_type not in ios_constants.SUPPORTED_TEST_TYPES:
      raise ios_errors.IllegalArgumentError(
          'The test type %s is not supported. Supported test types are %s.'
          % (self._test_type, ios_constants.SUPPORTED_TEST_TYPES))

  def _PrepareBuildProductsDir(self, built_products_dir):
    """Prepares the build products directory for dummy project.

    Args:
      built_products_dir: path of the directory to be prepared.
    """
    logging.info('Preparing build products directory %s for dummy project.',
                 built_products_dir)
    app_under_test_name = os.path.basename(self._app_under_test_dir)
    test_bundle_name = os.path.basename(self._test_bundle_dir)
    if not os.path.exists(
        os.path.join(built_products_dir, app_under_test_name)):
      shutil.copytree(self._app_under_test_dir,
                      os.path.join(built_products_dir, app_under_test_name))
    if not os.path.exists(
        os.path.join(built_products_dir, test_bundle_name)):
      shutil.copytree(self._test_bundle_dir,
                      os.path.join(built_products_dir, test_bundle_name))

  def _SetIosDeploymentTarget(self):
    """Sets the iOS deployment target in dummy project's pbxproj."""
    pbxproj_plist_obj = plist_util.Plist(self.pbxproj_file_path)
    pbxproj_plist_obj.SetPlistField(
        'objects:TestProjectBuildConfig:buildSettings:'
        'IPHONEOS_DEPLOYMENT_TARGET',
        bundle_util.GetMinimumOSVersion(self._app_under_test_dir))

  def _SetPbxprojForXcuitest(self):
    """Sets the dummy project's pbxproj for xcuitest."""
    pbxproj_plist_obj = plist_util.Plist(self.pbxproj_file_path)
    pbxproj_objects = pbxproj_plist_obj.GetPlistField('objects')

    # Sets the build setting for generated XCTRunner.app signing.
    # 1) If run with iphonesimulator, don't need to set any fields in build
    # setting. xcodebuild will sign the XCTRunner.app with identity '-' and no
    # provisioning profile by default.
    # 2) If runs with iphoneos and the test target app's embedded provisioning
    # profile is 'iOS Team Provisioning Profile: *', set build setting for using
    # Xcode managed provisioning profile to sign the XCTRunner.app.
    # 3) If runs with iphoneos and the test target app's embedded provisioning
    # profile is specific, set build setting for using app under test's
    # embedded provisioning profile to sign the XCTRunner.app. If the
    # provisioning profile is not installed in the Mac machine, also installs
    # it.
    if self._sdk == ios_constants.SDK.IPHONEOS:
      build_setting = pbxproj_objects[
          'XCUITestBundleBuildConfig']['buildSettings']
      build_setting['PRODUCT_BUNDLE_IDENTIFIER'] = bundle_util.GetBundleId(
          self._test_bundle_dir)
      build_setting['DEVELOPMENT_TEAM'] = bundle_util.GetDevelopmentTeam(
          self._test_bundle_dir)
      embedded_provision = provisioning_profile.ProvisiongProfile(
          os.path.join(self._app_under_test_dir, 'embedded.mobileprovision'),
          self._work_dir)
      embedded_provision.Install()
      # Case 2)
      if embedded_provision.name == 'iOS Team Provisioning Profile: *':
        build_setting['CODE_SIGN_IDENTITY'] = 'iPhone Developer'
      else:
        # Case 3)
        build_setting['CODE_SIGN_IDENTITY'] = bundle_util.GetCodesignIdentity(
            self._test_bundle_dir)
        (build_setting[
            'PROVISIONING_PROFILE_SPECIFIER']) = embedded_provision.name

    # Sets the app under test and test bundle.
    test_project_build_setting = pbxproj_objects[
        'TestProjectBuildConfig']['buildSettings']
    app_under_test_name = os.path.basename(
        self._app_under_test_dir).split('.')[0]
    pbxproj_objects['AppUnderTestTarget']['name'] = app_under_test_name
    pbxproj_objects['AppUnderTestTarget']['productName'] = app_under_test_name
    test_project_build_setting['APP_UNDER_TEST_NAME'] = app_under_test_name
    test_bundle_name = os.path.basename(self._test_bundle_dir).split('.')[0]
    pbxproj_objects['XCUITestBundleTarget']['name'] = test_bundle_name
    pbxproj_objects['XCUITestBundleTarget']['productName'] = test_bundle_name
    test_project_build_setting['XCUITEST_BUNDLE_NAME'] = test_bundle_name

    pbxproj_plist_obj.SetPlistField('objects', pbxproj_objects)

  def _SetPbxprojForXctest(self):
    """Sets the dummy project's pbxproj for xctest."""
    pbxproj_plist_obj = plist_util.Plist(self.pbxproj_file_path)
    pbxproj_objects = pbxproj_plist_obj.GetPlistField('objects')

    # Sets the build setting for test target app and unit test bundle signing.
    # 1) If run with iphonesimulator, don't need to set any fields in build
    # setting. xcodebuild will sign bundles with identity '-' and no
    # provisioning profile by default.
    # 2) If runs with iphoneos and the test target app's embedded provisioning
    # profile is 'iOS Team Provisioning Profile: *', set build setting for using
    # Xcode managed provisioning profile to sign bundles.
    # 3) If runs with iphoneos and the test target app's embedded provisioning
    # profile is specific, set build setting with using app under test's
    # embedded provisioning profile.
    if self._sdk == ios_constants.SDK.IPHONEOS:
      aut_build_setting = pbxproj_objects[
          'AppUnderTestBuildConfig']['buildSettings']
      test_build_setting = pbxproj_objects[
          'XCTestBundleBuildConfig']['buildSettings']
      aut_build_setting['CODE_SIGNING_REQUIRED'] = 'YES'
      aut_build_setting['PRODUCT_BUNDLE_IDENTIFIER'] = bundle_util.GetBundleId(
          self._app_under_test_dir)
      embedded_provision = provisioning_profile.ProvisiongProfile(
          os.path.join(self._app_under_test_dir, 'embedded.mobileprovision'),
          self._work_dir)
      embedded_provision.Install()
      # Case 2)
      if embedded_provision.name == 'iOS Team Provisioning Profile: *':
        aut_build_setting['CODE_SIGN_IDENTITY'] = 'iPhone Developer'
        test_build_setting['CODE_SIGN_IDENTITY'] = 'iPhone Developer'
        app_under_test_dev_team = bundle_util.GetDevelopmentTeam(
            self._app_under_test_dir)
        aut_build_setting['DEVELOPMENT_TEAM'] = app_under_test_dev_team
        test_build_setting['DEVELOPMENT_TEAM'] = app_under_test_dev_team
      else:
        # Case 3)
        app_under_test_sign_identity = bundle_util.GetCodesignIdentity(
            self._app_under_test_dir)
        aut_build_setting['CODE_SIGN_IDENTITY'] = app_under_test_sign_identity
        test_build_setting['CODE_SIGN_IDENTITY'] = app_under_test_sign_identity
        (aut_build_setting[
            'PROVISIONING_PROFILE_SPECIFIER']) = embedded_provision.name

    # Sets the app under test and test bundle.
    test_project_build_setting = pbxproj_objects[
        'TestProjectBuildConfig']['buildSettings']
    app_under_test_name = os.path.basename(
        self._app_under_test_dir).split('.')[0]
    pbxproj_objects['AppUnderTestTarget']['name'] = app_under_test_name
    pbxproj_objects['AppUnderTestTarget']['productName'] = app_under_test_name
    test_project_build_setting['APP_UNDER_TEST_NAME'] = app_under_test_name
    test_bundle_name = os.path.basename(self._test_bundle_dir).split('.')[0]
    pbxproj_objects['XCTestBundleTarget']['name'] = test_bundle_name
    pbxproj_objects['XCTestBundleTarget']['productName'] = test_bundle_name
    test_project_build_setting['XCTEST_BUNDLE_NAME'] = test_bundle_name

    pbxproj_plist_obj.SetPlistField('objects', pbxproj_objects)

  def SetEnvVars(self, env_vars):
    """Sets the additional environment variables in the dummy project's scheme.

    Args:
     env_vars: dict. Both key and value is string.
    """
    if not env_vars:
      return
    self.GenerateDummyProject()
    scheme_path = self.test_scheme_path
    scheme_tree = ET.parse(scheme_path)
    root = scheme_tree.getroot()
    test_action_element = root.find('TestAction')
    test_action_element.set('shouldUseLaunchSchemeArgsEnv', 'NO')
    envs_element = ET.SubElement(test_action_element, 'EnvironmentVariables')
    for key, value in env_vars.items():
      env_element = ET.SubElement(envs_element, 'EnvironmentVariable')
      env_element.set('key', key)
      env_element.set('value', value)
      env_element.set('isEnabled', 'YES')
    scheme_tree.write(scheme_path)

  def SetArgs(self, args):
    """Sets the additional arguments in the dummy project's scheme.

    Args:
     args: a list of string. Each item is an argument.
    """
    if not args:
      return
    self.GenerateDummyProject()
    scheme_path = self.test_scheme_path
    scheme_tree = ET.parse(scheme_path)
    test_action_element = scheme_tree.getroot().find('TestAction')
    test_action_element.set('shouldUseLaunchSchemeArgsEnv', 'NO')
    args_element = ET.SubElement(test_action_element, 'CommandLineArguments')
    for arg in args:
      arg_element = ET.SubElement(args_element, 'CommandLineArgument')
      arg_element.set('argument', arg)
      arg_element.set('isEnabled', 'YES')
    scheme_tree.write(scheme_path)


def _GetTestProject(work_dir):
  """Gets the TestProject path."""
  test_project_path = os.path.join(work_dir, 'Resource/TestProject')
  if os.path.exists(test_project_path):
    return test_project_path

  xcodeproj_path = os.path.join(test_project_path, 'TestProject.xcodeproj')
  os.makedirs(xcodeproj_path)
  with open(os.path.join(xcodeproj_path, 'project.pbxproj'),
            'w+') as target_file:
    target_file.write(
        pkgutil.get_data('XCTestRunner.TestRunner',
                         'TestProject/TestProject.xcodeproj/project.pbxproj'))
  xcschemes_path = os.path.join(xcodeproj_path, 'xcshareddata/xcschemes')
  os.makedirs(xcschemes_path)
  with open(os.path.join(xcschemes_path, 'TestProjectXctest.xcscheme'),
            'w+') as target_file:
    target_file.write(
        pkgutil.get_data(
            'XCTestRunner.TestRunner',
            'TestProject/TestProject.xcodeproj/xcshareddata/xcschemes/'
            'TestProjectXctest.xcscheme'))
  with open(os.path.join(xcschemes_path, 'TestProjectXcuitest.xcscheme'),
            'w+') as target_file:
    target_file.write(
        pkgutil.get_data(
            'XCTestRunner.TestRunner',
            'TestProject/TestProject.xcodeproj/xcshareddata/xcschemes/'
            'TestProjectXcuitest.xcscheme'))
  return test_project_path