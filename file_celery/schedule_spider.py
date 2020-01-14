from gevent import monkey
from gevent.pool import Pool

monkey.patch_all()

import importlib
from datetime import datetime

from celery import Task

import settings
from utils import logger
from celery_app import celery_app
from utils.proxy_check import check_proxy
from models import Session, Proxy, CeleryTask


class RunSpider:
    def __init__(self):
        self.coroutine_pool = Pool()

    def get_spider_obj_from_settings(self):
        """
        从 settings 文件中获取所有的具体爬虫的路径字符串
        将字符串进行分隔，得到具体爬虫的模块路径
        为每一个具体爬虫类创建一个对象
        使用协程池将每一个具体爬虫放在协程池中，使用异步方式的执行爬虫
        :return:
        """
        for full_name in settings.PROXIES_SPIDERS:
            # 从右边以 '.' 进行分隔，maxsplit 代表只分隔一次
            module_name, class_name = full_name.rsplit('.', maxsplit=1)

            # 导入具体爬虫所在的模块
            module = importlib.import_module(module_name)

            # 获取具体爬虫中的类名
            cls = getattr(module, class_name)

            # 创建具体爬虫的类对象
            spider = cls()

            yield spider

    def __execute_one_spider_task(self, spider):
        """
        一次执行具体爬虫的任务
        :param spider:
        :return:
        """
        # 异常处理，防止一个爬虫内部出错影响其它的爬虫
        try:
            # 遍历爬虫对象的 get_proxies 方法，返回每一个 代理 ip 对象
            for proxy in spider.get_proxies():
                # 检验代理 ip 的可用性
                proxy = check_proxy(proxy)

                # 如果 speed 不为 -1 说明可用，则保存到数据库中
                if proxy.speed != -1:
                    session = Session()
                    exist = session.query(Proxy) \
                        .filter(Proxy.ip == str(proxy.ip), Proxy.port == str(proxy.port)) \
                        .first()

                    if not exist:
                        obj = Proxy(
                            ip=str(proxy.ip),
                            port=str(proxy.port),
                            protocol=proxy.protocol,
                            nick_type=proxy.nick_type,
                            speed=proxy.speed,
                            area=str(proxy.area),
                            score=proxy.score,
                            disable_domain=proxy.disable_domain,
                            origin=str(proxy.origin),
                            create_time=datetime.now()
                        )
                        session.add(obj)
                        session.commit()
                        session.close()
                        logger.warning(f' insert: {proxy.ip}:{proxy.port} from {proxy.origin}')
                    else:
                        exist.score['score'] = settings.MAX_SCORE
                        exist.score['power'] = 0
                        exist.port = proxy.port
                        exist.protocol = proxy.protocol
                        exist.nick_type = proxy.nick_type
                        exist.speed = proxy.speed
                        exist.area = proxy.area
                        exist.disable_domain = proxy.disable_domain
                        exist.origin = proxy.origin
                        session.commit()
                        session.close()
                        logger.warning(f' already exist {proxy.ip}:{proxy.port}, update successfully')
                else:
                    logger.info(f' invalid {proxy.ip}:{proxy.port} from {proxy.origin}')

        except Exception as e:
            logger.error(f'scrapy error: {e}')

    def run(self):
        # 获取所有的具体爬虫对象
        spiders = self.get_spider_obj_from_settings()

        # 将每一个具体爬虫放入到协程池中，用函数引用的方式指向一次具体爬虫的任务
        for spider in spiders:
            self.coroutine_pool.apply_async(self.__execute_one_spider_task, args=(spider,))

        # 使用协程池的 join 方法，让当前线程等待协程池的任务完成
        self.coroutine_pool.join()

    @classmethod
    def start(cls):
        """
        使用 schedule 定时
        类方法，方便最后整合直接通过 类名.start() 方法去执行这个类里面的所任务
        :return:
        """
        # 创建当前类对象，使用当前类对象调用当前类中的 run 方法，执行所有的具体爬虫
        rs = cls()
        rs.run()


class CallbackTask(Task):

    def __init__(self):
        self.session = Session()

    def on_success(self, result, task_id, args, kwargs):
        """
        成功执行的函数
        :param result:
        :param task_id:
        :param args:
        :param kwargs:
        :return:
        """
        task = self.session.query(CeleryTask).filter(CeleryTask.task_id == task_id).first()
        task.end_time = datetime.now()
        task.task_status = 0
        task.harvest = self.session.query(Proxy).count() - task.harvest
        self.session.commit()
        self.session.close()

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """
        失败执行的函数
        :param self:
        :param exc:
        :param task_id:
        :param args:
        :param kwargs:
        :param einfo:
        :return:
        """
        task = self.session.query(CeleryTask).filter(CeleryTask.task_id == task_id).first()
        task.end_time = datetime.now()
        task.task_status = -1
        task.harvest = self.session.query(Proxy).count() - task.harvest
        self.session.commit()
        self.session.close()


@celery_app.task(base=CallbackTask)
def delay_spider():
    RunSpider.start()


@celery_app.task
def schedule_spider():
    task_id = delay_spider.delay()

    session = Session()
    celery_task = CeleryTask(
        task_id=task_id,
        task_name='spider',
        harvest=session.query(Proxy).count()
    )
    session.add(celery_task)
    session.commit()
    session.close()


if __name__ == '__main__':
    RunSpider.start()
