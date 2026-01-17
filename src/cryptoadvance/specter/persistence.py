""" Provides an API abstracting writes (and reads) on anything
    happening on the ~/.specter-folder. Useful as we can then
    call the call-back-method.
"""

import csv
import json
import logging
import os
import shutil
import tempfile
import threading


from flask import current_app as app

from cryptoadvance.specter.util.reflection import get_class

from .specter_error import SpecterError, SpecterInternalException
from .services.callbacks import specter_persistence_callback
from .util.shell import run_shell

logger = logging.getLogger(__name__)

fslock = threading.Lock()
pclock = threading.Lock()


class PersistentObject:
    """An object which contains reasonable infrastructure to get un-/persisted (in json)
    As such, other than the name implies, it doesn't contain any business specific attributes
    Its usage is not (yet?!) supported in this persistence module but let's see
    """

    @property
    def fqcn(self):
        """the fully qualified class Name, e.g. "cryptoadvance.specter.node.Node"""
        return f"{self.__class__.__module__}.{self.__class__.__name__}"

    @property
    def json(self):
        """A property to easily transform your BO to a dict. Use it like:
        mybo_json = super().json
        return deep_update(
            mybo_json,
            {
                "name": self.name,
                "alias": self.alias,
                [...]
            },
        )

        """
        self_json = {}
        self_json["python_class"] = self.fqcn
        if hasattr(self, "fullpath"):
            self_json["fullpath"] = self.fullpath
        return self_json

    @property
    def is_specter_core_object(self):
        """Whether the class is defined in cryptoadvance.specter"""
        return self.fqcn.startswith("cryptoadvance.specter.")

    @property
    def ext_id(self):
        """returns the third part of yourorg.specterext.ext_id"""
        if self.is_specter_core_object:
            return None
        else:
            return self.fqcn.split(".")[2]

    @property
    def blueprint(self):
        """returns the blueprint of the extension (assuming there is only a default one)"""
        if self.is_specter_core_object:
            return ""
        else:
            return f"{self.ext_id}_endpoint"

    @classmethod
    def from_json(cls, a_dict, *args, **kwargs):
        """Creates a PersistentObject of the right class
        Might throw a SpecterInternalException
        """
        try:
            clazz = get_class(a_dict["python_class"])
        except KeyError:
            raise SpecterInternalException("dict does not have a python_class")
        return clazz.from_json(a_dict, *args, **kwargs)


def read_json_file(path):
    """read_json_file from the .specter-directory. Don't use it for
    something else"""
    with fslock:
        bkp = path + ".bkp"
        # try reading file
        try:
            with open(path, "r") as f:
                content = json.load(f)

        # if failed - try reading from the backup
        except Exception as e:
            logger.exception(
                f"Exception {e} while reading file {path}. Reading from backup", e
            )
            # if no backup exists - raise
            if not os.path.isfile(bkp):
                raise e
            # try loading from backup
            with open(bkp, "r") as f:
                content = json.load(f)
            # recover from backup
            if os.path.isfile(path):
                os.remove(path)
            os.rename(bkp, path)
            logger.error(f"failed to open {path}, recovered from backup.")
        return content


def _delete_folder(path):
    """Internal method which won't trigger the callback"""
    with fslock:
        shutil.rmtree(path, ignore_errors=True)  # might not exist


def _write_json_file(content, path, lock=None):
    """Internal method which won't trigger the callback"""
    if lock is None:
        lock = fslock
    with lock:
        def _fsync_dir(dir_path: str):
            try:
                dir_fd = os.open(dir_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except Exception:
                return
            try:
                os.fsync(dir_fd)
            except Exception:
                pass
            finally:
                os.close(dir_fd)

        bkp = path + ".bkp"
        dir_path = os.path.dirname(path) or "."

        try:
            existing_stat = os.stat(path) if os.path.exists(path) else None

            # Write the new JSON content into a temp file in the same directory
            # so the final replace is atomic on the same filesystem.
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.basename(path) + ".tmp.", dir=dir_path
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(content, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())

                # Preserve basic permissions/ownership if the file already existed.
                if existing_stat is not None:
                    try:
                        os.chmod(tmp_path, existing_stat.st_mode & 0o777)
                    except Exception:
                        pass
                    try:
                        os.chown(tmp_path, existing_stat.st_uid, existing_stat.st_gid)
                    except Exception:
                        pass

                # Keep a backup of the previous version (atomic + durable).
                if os.path.isfile(path):
                    os.replace(path, bkp)
                    _fsync_dir(dir_path)

                # Atomically replace the target file.
                os.replace(tmp_path, path)
                _fsync_dir(dir_path)
            finally:
                # If anything above failed before os.replace(tmp_path, path), clean it up.
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

            # Verify the written file parses as JSON.
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)

        except Exception as e:
            logger.exception(e)
            # Restore from backup if possible.
            try:
                if os.path.isfile(bkp):
                    os.replace(bkp, path)
                    _fsync_dir(dir_path)
            except Exception:
                pass
            raise SpecterError(
                f"Error:{path} could not be saved. The old version has been restored (if available). Check the logs for details. This is probably a bug."
            )


def write_json_file(content, path, lock=None):
    _write_json_file(content, path, lock)
    storage_callback(path=path)


def delete_files(paths):
    """deletes multiple files and calls storage callback once"""
    for path in paths:
        try:
            os.remove(path)
            storage_callback(mode="delete", path=path)
        except FileNotFoundError:
            pass


def delete_file(path):
    delete_files([path])


def write_devices(devices_json):
    """interpret a json as a list of devices and write them in the devices subfolder inside the specter-folder"""
    for device_json in devices_json:
        path = os.path.join(
            app.specter.device_manager.data_folder, "%s.json" % device_json["alias"]
        )
        _write_json_file(device_json, path)
        storage_callback(path=path)


def write_wallet(wallet_json):
    """interpret a json as wallet and writes it in the wallets subfolder inside the specter-folder.
    overwrites it if existing.
    """
    fpath = os.path.join(
        app.specter.wallet_manager.working_folder, "%s.json" % wallet_json["alias"]
    )
    _write_json_file(wallet_json, fpath)
    storage_callback(path=fpath)


def write_device(device, fullpath):
    _write_json_file(device.json, fullpath)
    storage_callback(path=fullpath)


def write_node(node, fullpath):
    _write_json_file(node.json, fullpath)
    storage_callback(path=fullpath)


def delete_folder(path):
    _delete_folder(path)
    storage_callback(mode="delete", path=path)


def delete_folders(paths):
    for path in paths:
        _delete_folder(path)
        storage_callback(mode="delete", path=path)


def _write_csv(fname, objs, cls=dict):
    columns = []
    # if it's a custom class
    if hasattr(cls, "columns"):
        columns = cls.columns
    # if it's just a dict
    elif len(objs) > 0:
        columns = objs[0].keys()
    with fslock:
        with open(fname, mode="w") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for obj in objs:
                writer.writerow(obj)


def write_csv(fname, objs, cls=dict):
    _write_csv(fname, objs, cls)
    storage_callback(path=fname)


def read_csv(fname, cls=dict, *args):
    with fslock:
        with open(fname, mode="r") as csv_file:
            csv_reader = csv.DictReader(csv_file)
            return [cls(*args, **row) for row in csv_reader]


def storage_callback_function(cmd_list):
    """This is executing the callback-script, either directly or via threading"""
    with pclock:
        logger.debug(f"Executing {cmd_list}")
        result = run_shell(cmd_list)
        if result["code"] != 0:
            logger.error("callback failed ")
            logger.error("stderr: {}".format(result["err"]))
            logger.error("stdout: {}".format(result["out"]))
        else:
            logger.info("Successfully executed {}".format(" ".join(cmd_list)))
            logger.debug("result: {}".format(result))


def storage_callback(mode="write", path=None):
    """Call this whenever anything in the .specter directory changes. Be aware that we might store node-data in the specter-folder"""
    # Might be usefull to figure out why the callback has been triggered:
    # traceback.print_stack()
    # logger.debug(f"Storage Callback called mode {mode} with path {path}")

    # First, call extensions which want to get informed.
    # This is working synchronously!
    try:
        app.specter.service_manager.execute_ext_callbacks(
            specter_persistence_callback, path=path, mode=mode
        )
    except AttributeError as e:
        # chicken-egg poroblem:
        if str(e).endswith("object has no attribute 'specter'"):
            pass
        else:
            raise e
    except RuntimeError as e:
        if str(e).startswith("Working outside of application context"):
            # not yet (?!) supported
            pass
        else:
            raise e

    # Now maybe we have async scripts?
    if os.getenv("SPECTER_PERSISTENCE_CALLBACK_ASYNC"):
        cmd_list = os.getenv("SPECTER_PERSISTENCE_CALLBACK_ASYNC").split(" ")
        cmd_list.append(mode)
        cmd_list.append(path)
        t = threading.Thread(
            target=storage_callback_function,
            args=(cmd_list,),
        )
        t.start()
    # Or maybe even synchronous scripts?
    elif os.getenv("SPECTER_PERSISTENCE_CALLBACK"):
        cmd_list = os.getenv("SPECTER_PERSISTENCE_CALLBACK").split(" ")
        cmd_list.append(mode)
        cmd_list.append(path)
        storage_callback_function(cmd_list)
