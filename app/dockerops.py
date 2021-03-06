# coding:utf-8
# Software License Agreement (BSD License)
#
# Copyright (c) 2016, micROS Team
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# 
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# 
# * Neither the name of micROS-drt nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import zipfile, os, shutil, json, time, logging, ast

import docker
from app import db, models
from app.models import *
from app.commonset import *
from app.deploy_svc_create import *
from datetime import datetime
from flask_login import current_user
from flask.templating import render_template
from flask.globals import session
from Tkinter import image_names
from kubernetes import client, config

server_ip = '192.168.4.105'
registry = server_ip + ':5000'
current_milli_time = lambda: int(round(time.time() * 1000))


class StreamLineBuildGenerator(object):
    def __init__(self, json_data):
        self.__dict__ = json.loads(json_data)


def downloadFileBuild(downloadFileName):
    images = models.Image.query.filter_by(imagename=downloadFileName).first()
    subscribed_topics = StringToListOfDict(images.subscribed_topics)
    published_topics = StringToListOfDict(images.published_topics)
    advertised_services = StringToList(images.advertised_services)
    image_name = images.imagename

    '''Generating client-side proxy'''
    client_path = './client/'
    download_path = './app/download'
    try:
        if os.path.exists(client_path):
            shutil.rmtree(client_path)
        if os.path.exists(download_path):
            shutil.rmtree(download_path)
        os.mkdir(download_path)
        rysnc_cmd = 'rsync -aP cloudproxy ' + client_path
        os.system(rysnc_cmd)
        client_url = url()
        ########
        pub_topic_list = []
        sub_topic_list = []
        for pub_topic in published_topics:
            pub_topic_list.append(pub_topic.get("topic_name"))
        for sub_topic in subscribed_topics:
            sub_topic_list.append(sub_topic.get("topic_name"))

        compress_list = []
        for pub_topic in published_topics:
            pub_topic_name = pub_topic.get("topic_name")
            pub_topic_name_c = pub_topic_name + '/repub'
            pub_node_name = pub_topic_name + '_compress'
            compressed = pub_topic.get("compression")
            raw = 'raw'
            if compressed != 'none':
                compress_list.append(
                    '<node pkg="image_transport" type="republish" name="%s" args="%s in:=%s %s out:=%s">' % (
                        pub_node_name, compressed, pub_topic_name_c, raw, pub_topic_name))

        for sub_topic in subscribed_topics:
            sub_topic_name = sub_topic.get("topic_name")
            sub_topic_name_c = sub_topic_name + '/repub'
            sub_node_name = sub_topic_name + '_compress'
            compressed = sub_topic.get("compression")
            raw = 'raw'
            if compressed != 'none':
                compress_list.append(
                    '<node pkg="image_transport" type="republish" name="%s" args="%s in:=%s %s out:=%s">' % (
                        sub_node_name, raw, sub_topic_name, compressed, sub_topic_name_c))

        client_launch = render_template('client.launch', published_topics=sub_topic_list,
                                        subscribed_topics=pub_topic_list,
                                        advertised_services=advertised_services, url=client_url, image_id=image_name,
                                        compress_list=compress_list)
        with open("./client/cloudproxy/launch/client.launch", "wb") as fh:
            fh.write(client_launch)
        ########
        zip_cmd = 'zip -r ./client/' + image_name + ".zip " + "./client/cloudproxy/"
        os.system(zip_cmd)
        os.system("mv ./client/" + image_name + ".zip ./app/download/")

    except Exception, e:
        error_string = 'Unable to generating client proxy for image {}. \nReason: {}'.format(image_name, str(e))
        logging.error(error_string)
        return error_string
    return None


def uploadFile(ros_file, manifest_file, comments):
    upload_path = './upload'
    logging.info('Uploading %s to path %s', ros_file.filename, upload_path)

    '''The internal unique id of a uploaded file will be the mill time since 1970'''
    image_name = str(current_milli_time())
    save_filename = image_name + '.zip'

    '''Save file to the upload directory. Replace filename with the internal unique id'''
    try:
        if not os.path.exists(upload_path):
            os.mkdir(upload_path)

        ros_file.save(os.path.join(upload_path, save_filename))
    except Exception, e:
        error_string = 'Unable to save file {} to path {}. \nReason: {}'.format(save_filename, upload_path, str(e))
        logging.error(error_string)
        return error_string

    logging.info('Unzipping uploaded file %s', ros_file.filename)

    '''Unzip the uploaded file'''
    temp_path = './temp'
    try:
        if os.path.exists(temp_path):
            shutil.rmtree(temp_path)

        unzip_cmd = 'unzip ' + os.path.join(upload_path, save_filename) + ' -d ' + temp_path
        os.system(unzip_cmd)
    except Exception, e:
        error_string = 'Unzip file {} to path {} failure. \nReason: {}'.format(save_filename, temp_path, str(e))
        logging.error(error_string)
        return error_string

    manifest = json.load(manifest_file)
    published_topics = manifest.get('published_topics')
    subscribed_topics = manifest.get(
        'subscribed_topics')  # "subscribed_topics": [{"topic_name":"/tf","compression":"none"},{"topic_name":"/base_scan","compression":"compressed"}],\
    advertised_services = manifest.get('advertised_services')
    advertised_actions = manifest.get('advertised_actions')
    start_cmds = manifest.get('start_cmds')
    external_lib_paths = manifest.get('external_lib_paths', [])
    mem_limit = manifest.get('mem_limit')
    memswap_limit = manifest.get('memswap_limit')
    cpushares = manifest.get('cpushares')
    cpusetcpus = manifest.get('cpusetcpus')
    container_limits = {'memory': int(mem_limit), 'memswap': int(memswap_limit), 'cpushares': int(cpushares),
                        'cpusetcpus': cpusetcpus}

    if start_cmds == None:
        error_string = 'Manifest file {} does not contain start_cmds. Generate docker image failed'.format(
            manifest_file.getFileName())
        logging.error(error_string)
        return error_string
    rosentry = render_template('ros_entry.sh', start_cmds=start_cmds, external_lib_paths=external_lib_paths)
    with open("./temp/ros_entry.sh", "wb") as fh:
        fh.write(rosentry)

    ####   
    compress_list = []
    for pub_topic in published_topics:
        pub_topic_name = pub_topic.get("topic_name")
        pub_topic_name_c = pub_topic_name + '/repub'
        pub_node_name = pub_topic_name + '_compress'
        compressed = pub_topic.get("compression")
        raw = 'raw'
        if compressed != 'none':
            compress_list.append(
                '<node pkg="image_transport" type="republish" name="%s" args="%s in:=%s %s out:=%s">' % (
                    pub_node_name, raw, pub_topic_name, compressed, pub_topic_name_c))

    for sub_topic in subscribed_topics:
        sub_topic_name = sub_topic.get("topic_name")
        sub_topic_name_c = sub_topic_name + '/repub'
        sub_node_name = sub_topic_name + '_compress'
        compressed = sub_topic.get("compression")
        raw = 'raw'
        if compressed != 'none':
            compress_list.append(
                '<node pkg="image_transport" type="republish" name="%s" args="%s in:=%s %s out:=%s">' % (
                    sub_node_name, compressed, sub_topic_name_c, raw, sub_topic_name))

    topic_compress = render_template('compression.launch', compress_list=compress_list)
    with open("./temp/compression.launch", "wb") as fh:
        fh.write(topic_compress)
    ####

    '''Building the docker image'''
    logging.info('Generating docker image with tag %s', image_name)
    try:
        # docker_client = docker.from_env()
        docker_client = docker.APIClient(base_url='unix://var/run/docker.sock')
        registry_imagename = registry + '/' + image_name
        '''
        docker_client.images.build(path=".", rm=True, tag=registry_imagename, container_limits=container_limits)

    except docker.errors.BuildError as e:
        error_string = 'Unable to build docker image with name {}. \nReason: {}'.format(registry_imagename, str(e))
        logging.error(error_string)
        return error_string
    except docker.errors.APIError as e:
        error_string = 'Unable to build docker image with name {}. \nReason: {}'.format(registry_imagename, str(e))
        logging.error(error_string)
        return error_string

    # Push the images to private repository
    try:
        docker_client.images.push(registry_imagename, stream=True)
    except docker.errors.APIError as e:
        error_string = 'Unable to push the image {} to private registry. \nReason: {}'.format(image_name, str(e))
        logging.error(error_string)
        return error_string'''
        generator = docker_client.build(path=".", rm=True, tag=registry_imagename,
                                        container_limits=container_limits)  # 在ros:my基础上生成的新镜像，

        '''Check any error by inspecting the output of build()'''
        for line in generator:
            try:
                stream_line = StreamLineBuildGenerator(line)
                if hasattr(stream_line, "error"):  # hasattr(object, name) 函数用于判断对象是否包含对应的属性。如果对象有该属性返回 True，否则返回 False。
                    error_string = 'Unable to generating docker image with name {}. \nReason: {}'.format(
                        registry_imagename,
                        stream_line.error)
                    logging.error(error_string)
                    return error_string
            except ValueError:
                ''' If we are not able to deserialize the received line as JSON object, just ignore it'''
                continue
        '''Push the images to private repository'''
        response_push = docker_client.push(registry_imagename, stream=True)
        '''Check any error by inspecting the output of push()'''
        for line in response_push:
            try:
                json_line = json.loads(line)
                if 'error' in json_line.keys():
                    error_string = 'Unable to push docker image with name {}. \nReason: {}'.format(registry_imagename,
                                                                                                   json_line['error'])
                    logging.error(error_string)
                    return error_string
            except ValueError:
                ''' If we are not able to deserialize the received line as JSON object, just ignore it'''
                continue

    except Exception, e:
        error_string = 'Unable to generating docker image with name {}. \nReason: {}'.format(image_name, str(e))
        logging.error(error_string)
        return error_string

    shutil.rmtree(temp_path)  # shutil.rmtree() 表示递归删除文件夹下的所有子文件夹和子文件。

    '''Insert a new record to the image table in the database'''
    image_record = Image(imagename=image_name, uploadname=ros_file.filename, comments=comments,
                         uploadtime=datetime.now(), uploaduser=current_user.email,
                         published_topics=ListOfDictToString(published_topics),
                         subscribed_topics=ListOfDictToString(subscribed_topics),
                         advertised_services=ListToString(advertised_services),
                         advertised_actions=ListToString(advertised_actions))
    db.session.add(image_record)
    db.session.commit()

    logging.info('Uploading file %s to robotcloud successfully!', ros_file.filename)

    return "None;" + image_name


'''  for docker swarm
def getServicePort(image_name):
    logging.info('Starting a new services with image %s', image_name)

    try:
        image = registry + '/' + image_name
        com_cre_ser = 'docker service create --replicas 1  --publish ' + ':9090 ' + image
        service_ps = os.popen(com_cre_ser).read().split('\n')
        service_id = service_ps[0]
        time.sleep(5)
        ser_ins = "docker service inspect " + service_id
        ser_ins_ = json.loads(os.popen(ser_ins).read())
        port = ser_ins_[0]["Endpoint"]["Ports"][0]["PublishedPort"]

        get_node = models.ServerIP.query.first()
        ip = get_node.serverip
    except Exception, e:
        logging.error('Unable to create the service with image %s. \nReason: %s', image_name, str(e))
        return
    logging.info('Store the service infomation to the db')
    try:
        imageinfo = models.Image.query.filter_by(imagename=image_name).first()
        uploadn = imageinfo.uploadname
        usern = imageinfo.uploaduser
        service_record = Service(serviceid=service_id, createdtime=str(time.time()), imagename=image_name,
                                 uploadname=uploadn, username=usern, firstcreatetime=datetime.now())
        db.session.add(service_record)
        db.session.commit()
    except Exception, e:
        logging.error('Failed to store the service info to the db. \nReason: %s', str(e))
        return

    return ip + ':' + str(port) + " " + service_id'''


def getServicePort(image_name, node_port):
    logging.info('Starting a new k8s deployment and services with image %s', image_name)

    try:
        image = registry + '/' + image_name
        # 仅测试使用 image = "ros:test"
        deployment_name = "cloudroid"  # 无集群代码中可以直接指定，有集群代码需区分
        # k8s基本配置
        config.load_kube_config()
        apps_v1_api = client.AppsV1Api()
        # 后期若想提升性能或可靠性，可以增加replica
        create_deployment(apps_v1_api, image=image, deployment_name=deployment_name, label={"app": deployment_name})
        create_service(deployment_name=deployment_name, label={"app": deployment_name})

    except Exception, e:
        logging.error('Unable to create the deployment and service with image %s. \nReason: %s', image_name, str(e))
        return

    try:
        # 获取所创建service的cluster_ip
        cluster_ip = get_clusterip(
            deployment_name=deployment_name)  # list_service().items 是list类型   -----> k8s的Python返回值中，[]代表列表，{}代表类
        # 通过labels找到pod,再找到其host-IP
        label_selector = "app=" + deployment_name
        node_ip = get_nodeip(deployment_name=deployment_name, label_selector=label_selector)
        # 在不使用集群时，node_port可以固定，node_ip就是k8s所在主机ip
    except Exception, e:
        logging.error('Unable to get information from deployment or service. \nReason: %s', str(e))
        return

    # 修改nginx配置文件（Ubuntu下，centos下不同）
    # 后期涉及云-边-端协同时，最好是pod在哪个节点上，就在哪个节点上配置nginx，保证边端通信不涉及云端。当然这是建立在如下假设上：外部访问集群中的node的clusterip不用通过云端server。
    nginx_config = render_template('nginx.config', node_port=node_port, node_ip=node_ip, cluster_ip=cluster_ip)
    with open("/etc/nginx/sites-available/default", "wb") as fh:
        fh.write(nginx_config)
    reload_nginx_cmd = "systemctl reload nginx"
    value = os.system(reload_nginx_cmd)
    if value:
        logging.error('Unable to config nginx for reverse proxy.')
        return

    logging.info('Store the deployment and service information to the db')
    try:
        # 老版的数据库需要更新。基于此service新创建一个deployment的表，再创建一个k8s service的表。由于会有多用户同时连接，考虑使用mysql取代sqllite
        imageinfo = models.Image.query.filter_by(imagename=image_name).first()
        uploadn = imageinfo.uploadname
        usern = imageinfo.uploaduser
        deployment_record = Deployment(deployment_name=deployment_name, createdtime=str(time.time()),
                                       imagename=image_name,
                                       uploadname=uploadn, username=usern, firstcreatetime=datetime.now(),
                                       nodeip=node_ip)
        db.session.add(deployment_record)
        db.session.commit()

    except Exception, e:
        logging.error('Failed to store the service info to the db. \nReason: %s', str(e))
        return
    return node_ip + ':' + str(node_port) + " " + deployment_name


def deploymentinfo():
    logging.info('The query of services info list')

    try:
        services = models.Deployment.query.all()
        result = []
        part_line = {'deploymentname': 'default', 'imagename': 'default', 'filename': 'default', 'user': 'default',
                     'createtime': 'default', 'nodeip': 'default'}
        for i in services:
            part_line['deploymentname'] = i.deployment_name
            part_line['imagename'] = i.imagename
            part_line['filename'] = i.uploadname
            part_line['user'] = i.username
            part_line['createtime'] = i.firstcreatetime
            part_line['nodeip'] = i.nodeip
            result.append(part_line)
            part_line = {}

        return result

    except Exception, e:
        logging.error('Unable to list the services info. \nReason: %s', str(e))
        return


def removeDeployment(deployment_name):
    logging.info('Remove the deployment and service: %s', deployment_name)
    try:
        if deployment_name in get_exist_deployment():
            delete_deployment(deployment_name=deployment_name)
            delete_service(deployment_name=deployment_name)
        deployment_in_database = models.Deployment.query.all()
        # deployment_name_in_database = [x.deployment_name for x in deployment_in_database]
        for i in deployment_in_database:
            if i.deployment_name == deployment_name:
                db.session.delete(i)
                db.session.commit()
                break
    except Exception as e:
        logging.error('Unable to remove the deployment and service %s. \nReason: %s', deployment_name, str(e))

    '''try:
        docker_client = docker.from_env()
        docker_remove = docker_client.services.get(serviceid)
        docker_remove.remove()
        remove_ser = models.Service.query.all()
        for i in remove_ser:
            if (i.serviceid == serviceid):
                db.session.delete(i)
                db.session.commit()
                break

    except docker.errors.APIError as e:
        if e.status_code == 404:
            remove_ser = models.Service.query.all()
            for i in remove_ser:
                if (i.serviceid == serviceid):
                    db.session.delete(i)
                    db.session.commit()
                    break
        else:
            logging.error('Unable to remove the service %s. \nReason: %s', serviceid, str(e))'''


def deleteImage(image_name):
    logging.info('Delete the image %s', image_name)
    try:
        docker_client = docker.from_env()
        registry_imagename = registry + '/' + image_name
        docker_client.images.remove(image=registry_imagename, force=True)
        image = models.Image.query.filter_by(imagename=image_name).first()
        db.session.delete(image)
        db.session.commit()
    except docker.errors.APIError as e:
        image = models.Image.query.filter_by(imagename=image_name).first()
        db.session.delete(image)
        db.session.commit()
        error_string = 'Unable to delete the image {}. \nReason: {}. Delete the record'.format(registry_imagename,
                                                                                               str(e))
        logging.error(error_string)
        return error_string

    return None


def ListOfDictToString(lista):
    if lista.__len__() == 0:
        stringa = "None"
        return stringa
    else:
        stringa = ""
        for i in range(0, lista.__len__()):
            if (str(lista[i]).split("#")).__len__() <= 1:
                lista[i] = str(lista[i]) + "#"
                logging.info(lista[i])
        stringa = stringa.join(lista)
        logging.info(stringa)
        return stringa


def StringToListOfDict(stringa):
    if not stringa or stringa == 'None':
        lista = []
        return lista
    else:
        lista = stringa.split('#')
        listb = []
        for i in range(0, lista.__len__()):
            logging.info(lista[i])
            if lista[i] != "":
                listb.append(ast.literal_eval(lista[i]))
        # listb.pop()
        return listb


def ListToString(lista):
    if lista.__len__() == 0:
        stringa = "None"
        return stringa
    else:
        stringa = ""
        for i in range(0, lista.__len__()):
            if (lista[i].split("#")).__len__() <= 1:
                lista[i] = str(lista[i] + "#")
        stringa = stringa.join(lista)
        return stringa


def StringToList(stringa):
    if not stringa or stringa == 'None':
        lista = []
        return lista
    else:
        lista = stringa.split('#')
        lista.pop()
        return lista


if __name__ == "__main__":
    getServicePort_test(image_name="ros:test")
