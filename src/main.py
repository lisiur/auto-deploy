#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import requests
import datetime
import platform
import functools
from configparser import ConfigParser
from selenium import webdriver
from subprocess import call
from time import sleep
from bs4 import BeautifulSoup
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

cfg = ConfigParser()
cfg.read('../config.ini')

# project config
project_path = cfg.get('project', 'path')
project_name = cfg.get('project', 'name')

# gitlab config
gitlab_username = cfg.get('gitlab', 'username')
gitlab_password = cfg.get('gitlab', 'password')
gitlab_protocol = cfg.get('gitlab', 'protocol')
gitlab_host = cfg.get('gitlab', 'host')
gitlab_port = cfg.get('gitlab', 'port')
gitlab_origin = '{}://{}:{}'.format(gitlab_protocol, gitlab_host, gitlab_port)
login_url = gitlab_origin + '/users/sign_in'
tags_url = gitlab_origin + project_path + '/tags'
new_tag_url = gitlab_origin + project_path + '/tags/new'
pipelines_url = gitlab_origin + project_path + '/pipelines'

# marathon config
marathon_username = cfg.get('marathon', 'username')
marathon_password = cfg.get('marathon', 'password')
marathon_protocol = cfg.get('marathon', 'protocol')
marathon_host = cfg.get('marathon', 'host')
marathon_port = cfg.get('marathon', 'port')
marathon_origin = '{}://{}:{}'.format(marathon_protocol, marathon_host, marathon_port)
marathon_auth_url = '{}://{}:{}@{}:{}/ui'.format(marathon_protocol, marathon_username, marathon_password, marathon_host, marathon_port)
marathon_app_url = '{}://{}:{}/ui/#/apps?filterText={}'.format(marathon_protocol, marathon_host, marathon_port, project_name)

sysstr = platform.system()
driver_path = None
if sysstr == 'Windows':
    driver_path = '../driver/chromedriver.exe'
elif sysstr == 'Darwin':
    driver_path = '../driver/chromedriver'
elif sysstr == 'Linux':
    chrome_path = '../driver/chromedriver_linux'
else:
    raise OSError('{} is not supported'.format(sysstr))

chrome_options = webdriver.ChromeOptions()
chrome_options.add_argument("--headless")
wd = webdriver.Chrome(executable_path=driver_path, chrome_options=chrome_options)

session = None
processing_json_path = None
new_tag_version = None


def log(text):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kw):
            print('%s' % text)
            return func(*args, **kw)
        return wrapper
    return decorator


def get_format_time():
    return datetime.datetime.now().strftime('%H:%M:%S')


def logger(log_info):
    print('[{}] {}'.format(get_format_time(), log_info))


def get_cookies():
    logger('开始模拟登录 gitlab')

    wd.get(login_url)
    wd.find_element_by_xpath('//*[@id="user_login"]').send_keys(gitlab_username)
    wd.find_element_by_xpath('//*[@id="user_password"]').send_keys(gitlab_password)
    wd.find_element_by_xpath('//*[@id="new_user"]/div[2]/input').click()
    cookies = wd.get_cookies()

    logger('成功获取 cookies')
    return cookies


def login_with_cookies(cookies):
    # logger('开始构造 session')
    global session
    session = requests.Session()
    session.headers.clear()
    for cookie in cookies:
        session.cookies.set(cookie['name'], cookie['value'])

    logger('成功构造 session')
    return session


def login():
    global session
    if session:
        return session
    else:
        return login_with_cookies(get_cookies())


def get_latest_tag():
    global session
    session = login()

    logger('开始获取最新 tag')
    tags_html = session.get(tags_url).text
    soup = BeautifulSoup(tags_html, "html.parser")
    tag_div = soup.find_all(name='div', attrs={"class": "tags"})[0]
    tag_item = tag_div.find_all(name='i', attrs={"class": "fa-tag"})[0].next_sibling.string.strip()
    logger('成功获取最新 tag: {}'.format(tag_item))
    return tag_item


def increase_tag_version(tag_version):
    pre_version = tag_version
    splice = pre_version.split('.')
    splice[-1] = str(int(splice[-1]) + 1)
    next_version = '.'.join(splice)
    logger('构造目标 tag: {}'.format(next_version))
    return next_version


def create_new_tag():
    global new_tag_version
    old_tag_version = get_latest_tag()
    new_tag_version = increase_tag_version(old_tag_version)

    logger('开始打 tag')
    wd.get(new_tag_url)
    wd.find_element_by_xpath('//*[@id="tag_name"]').send_keys(new_tag_version)
    wd.find_element_by_xpath('//*[@id="new-tag-form"]/div[5]/button').click()
    logger('成功打 tag: {}'.format(new_tag_version))


def get_processing_json_path():
    global processing_json_path
    global session

    if processing_json_path:
        pass
    else:
        session = login()
        pipelines_html = session.get(pipelines_url).text
        soup = BeautifulSoup(pipelines_html, "html.parser")
        pipelines_dom = soup.find_all(name='ul', attrs={"class": "pipelines"})[0]
        commit_dom = pipelines_dom.find_all(name='td', attrs={"class": "commit-link"})[0]
        builds_path = gitlab_origin + commit_dom.find(name='a').attrs['href']
        build_tag_dom = BeautifulSoup(session.get(builds_path).text, "html.parser").find_all(attrs={"class": "builds-container"})[1]
        processing_json_path = gitlab_origin + build_tag_dom.find(attrs={"class": "build-content"}).find(name="a").attrs["href"] + '.json'

        wd.get(gitlab_origin + build_tag_dom.find(attrs={"class": "build-content"}).find(name="a").attrs["href"])

    return processing_json_path


def get_processing_json_data():
    global session
    global processing_json_path
    at_least_len = 3000

    session = login()
    processing_json_path = get_processing_json_path()

    logger('获取最新 build 日志: building...')
    json_data = session.get(processing_json_path).text
    json_head_data = json_data[:at_least_len]
    json_tail_data = json_data[-at_least_len:]

    logger('成功获取 build 日志: ' + json_tail_data[-100:])
    return json_head_data, json_tail_data


def watch_build_log():
    logger('开始监控 build 日志')
    global new_tag_version

    def watch():
        target_image_path = None
        reg_str = r'pushing (.*?' + new_tag_version + r'_.*?)\\u003cbr\\u003e'

        while True:
            build_head_log, build_tail_log = get_processing_json_data()

            if not target_image_path:
                if re.search(reg_str, build_head_log):
                    target_image_path = re.search(reg_str, build_head_log).group(1)
                    logger('最新镜像地址: {}'.format(target_image_path))

            if "Build succeeded" in build_tail_log:
                if target_image_path:
                    logger('镜像构建成功: {}'.format(target_image_path))
                    return target_image_path
                else:
                    raise EOFError('未匹配到镜像地址')

            else:
                sleep(30)

    return watch()


def watch_deploy_result(watch_url):
    wd.get(marathon_auth_url)
    watch_deploy_result_xpath = '//*[@id="marathon"]/div/div/div/div[1]/span/span[1]'
    deploy_origin_xpath = '//*[@id="marathon"]/div/div/div/div[2]/div/div/div[2]/table/tbody/tr/td[2]/a[2]'
    wd.get(watch_url)
    try:
        WebDriverWait(wd, 5).until(EC.visibility_of_element_located((By.XPATH, watch_deploy_result_xpath)), 'timed out')
        sleep(2)
        while True:
            status = wd.find_element_by_xpath(watch_deploy_result_xpath).text
            origin = wd.find_element_by_xpath(deploy_origin_xpath).text
            if status == 'Running':
                logger('部署成功')
                logger(origin)
                if sysstr == 'Darwin':
                    cmd = 'display notification \"' + origin + '\" with title \"部署成功\"'
                    call(["osascript", "-e", cmd])
                break
            else:
                logger('部署中...')
                sleep(5)

    except TimeoutException:
        print('timed out')

    wd.quit()


def update_marathon(image_path):
    wd.get(marathon_auth_url)
    wd.get(marathon_app_url)

    app_entrance_xpath = '//*[@id="marathon"]/div/div/div/div/main/div[2]/table/tbody/tr[4]/td[1]'
    config_tab_xpath = '//*[@id="marathon"]/div/div/div/div[2]/ul/li[2]/a'
    edit_button_xpath = '//*[@id="marathon"]/div/div/div/div[2]/div/div/div[1]/button'
    docker_container_xpath = '//*[@id="marathon"]/div/div[2]/div[1]/div/div/form/div[2]/div/ul/li[2]/label'
    image_input_xpath = '//*[@id="dockerImage"]'
    confirm_button_xpath = '//*[@id="marathon"]/div/div[2]/div[1]/div/div/form/div[3]/div/button[2]'

    def promise_click(xpath):
        try:
            WebDriverWait(wd, 5).until(EC.visibility_of_element_located((By.XPATH, xpath)), 'timed out')
        except TimeoutException:
            print('timed out')

        wd.find_element_by_xpath(xpath).click()

    promise_click(app_entrance_xpath)
    promise_click(config_tab_xpath)
    promise_click(edit_button_xpath)
    promise_click(docker_container_xpath)

    try:
        WebDriverWait(wd, 5).until(EC.visibility_of_element_located((By.XPATH, image_input_xpath)), 'timed out')
    except TimeoutException:
        print('timed out')

    wd.find_element_by_xpath(image_input_xpath).clear()
    wd.find_element_by_xpath(image_input_xpath).send_keys(image_path)

    wd.find_element_by_xpath(confirm_button_xpath).click()

    watch_url = wd.current_url.split('/configuration')[0]
    logger('开始部署...')
    watch_deploy_result(watch_url)


def run():
    create_new_tag()
    update_marathon(watch_build_log())


if __name__ == '__main__':
    run()
