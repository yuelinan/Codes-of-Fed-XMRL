
import torch
import torch.optim as optim
import torch.nn.functional as F
from torch_geometric.loader import DataLoader
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm
from datetime import datetime
## dataset
from sklearn.model_selection import train_test_split
from ogb.graphproppred import PygGraphPropPredDataset, Evaluator
import random
## training
from model import F2XMRL
from utils import init_weights, get_args, eval_test,train_xmrl,get_kmeans
import copy

def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

args_first = get_args()
import logging
logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.DEBUG,  #INFO,
                    )
logger = logging.getLogger(__name__)

def main(args,seed):
    print(args)
    device = torch.device("cuda:" + str(args.device)) if torch.cuda.is_available() else torch.device("cpu")
    if args.dataset.startswith('ogbg'):
        dataset = PygGraphPropPredDataset(name = args.dataset, root='data')
        
        split_idx = dataset.get_idx_split()
        
        train = dataset[split_idx["train"]]
        print(len(train))

        ## split the client
        random.seed(42)
        np.random.seed(42)

        def partition_class_samples_with_dirichlet_distribution(N, alpha, client_num, idx_batch, idx_k):
            np.random.shuffle(idx_k)
            # using dirichlet distribution to determine the unbalanced proportion for each client (client_num in total)
            # e.g., when client_num = 4, proportions = [0.29543505 0.38414498 0.31998781 0.00043216], sum(proportions) = 1
            proportions = np.random.dirichlet(np.repeat(alpha, client_num))
            print(proportions)
            weight = proportions
            # get the index in idx_k according to the dirichlet distribution
            proportions = np.array([p * (len(idx_j) < N / client_num) for p, idx_j in zip(proportions, idx_batch)])
            proportions = proportions / proportions.sum()
            proportions = (np.cumsum(proportions) * len(idx_k)).astype(int)[:-1]

            # generate the batch list for each client
            idx_batch = [idx_j + idx.tolist() for idx_j, idx in zip(idx_batch, np.split(idx_k, proportions))]
            min_size = min([len(idx_j) for idx_j in idx_batch])

            return idx_batch, min_size,weight

        def create_non_uniform_split(alpha, idxs, client_number, is_train=True):
            
            N = len(idxs)
            logging.info("sample number = %d, client_number = %d" % (N, client_number))
            logging.info(idxs)
            idx_batch_per_client = [[] for _ in range(client_number)]
            (
                idx_batch_per_client,
                min_size,
                weight,
            ) = partition_class_samples_with_dirichlet_distribution(
                N, alpha, client_number, idx_batch_per_client, idxs
            )
            logging.info(idx_batch_per_client)
            sample_num_distribution = []

            for client_id in range(client_number):
                sample_num_distribution.append(len(idx_batch_per_client[client_id]))
                logging.info(
                    "client_id = %d, sample_number = %d"
                    % (client_id, len(idx_batch_per_client[client_id]))
                )
            return idx_batch_per_client,weight

        def get_fed_dataset(train,client_number,alpha ):
            num_train_samples = len(train)
            train_idxs = list(range(num_train_samples))
            random.shuffle(train_idxs)

            clients_idxs_train,weight = create_non_uniform_split(
            alpha,  train_idxs, client_number, True
            )
            # print(clients_idxs_train)
            partition_dicts = [None] * client_number

            for client in range(client_number):
                client_train_idxs = clients_idxs_train[client]

                train_client = [
                    train[idx] for idx in client_train_idxs
                ]
                # print(train_client)
                train_loader = DataLoader(train_client, batch_size=args.batch_size, shuffle=True, num_workers = 0)

                partition_dict = {
                "train": train_loader,
                }

                partition_dicts[client] = partition_dict

            return partition_dicts,weight

        partition_dicts,client_weights = get_fed_dataset(train, client_number=args.client_number, alpha=args.alpha)

        valid_loader = DataLoader(dataset[split_idx["valid"]], batch_size=args.batch_size, shuffle=False, num_workers = 0)
        test_loader = DataLoader(dataset[split_idx["test"]], batch_size=args.batch_size, shuffle=False, num_workers = 0)
        evaluator = Evaluator(args.dataset)

    set_seed(seed)

    # n_train_data, n_val_data, n_test_data = len(train_loader.dataset), len(valid_loader.dataset), float(len(test_loader.dataset))
    # logger.info(f"# Train: {n_train_data}  #Test: {n_test_data} #Val: {n_val_data}")
    print(dataset.num_tasks)

    server_model = eval(args.model_name)( gnn_type = args.gnn, num_tasks = dataset.num_tasks, num_layer = args.num_layer,
                         emb_dim = args.emb_dim, drop_ratio = args.drop_ratio, gamma=args.gamma, use_linear_predictor = args.use_linear_predictor).to(device)   
                          
    init_weights(server_model, args.initw_name, init_gain=0.02)

    models = [copy.deepcopy(server_model) for idx in range(args.client_number)]


    def get_opt(args,model):
    
        opt_separator = optim.Adam(model.separator.parameters(), lr=args.lr, weight_decay=args.l2reg)
        opt_predictor = optim.Adam(list(model.graph_encoder.parameters())+list(model.predictor.parameters()), lr=args.lr, weight_decay=args.l2reg)

        optimizers = {'separator': opt_separator, 'predictor': opt_predictor}
        if args.use_lr_scheduler:
            schedulers = {}
            for opt_name, opt in optimizers.items():
                schedulers[opt_name] = optim.lr_scheduler.CosineAnnealingLR(opt, T_max=100, eta_min=1e-4)
        else:
            schedulers = None
        return optimizers,schedulers

    def communication(server_model, models, client_weights):
        client_num = len(models)
        with torch.no_grad():
            for key in server_model.state_dict().keys():
                temp = torch.zeros_like(server_model.state_dict()[key], dtype=torch.float32)
                # 参数聚合
                # print(client_num)
                for client_idx in range(client_num):
                    #  print(client_weights[client_idx])
                    temp += client_weights[client_idx] * models[client_idx].state_dict()[key]
                server_model.state_dict()[key].data.copy_(temp)
                # 参数分发
                for client_idx in range(client_num):
                    models[client_idx].state_dict()[key].data.copy_(server_model.state_dict()[key])
        return server_model, models

    cnt_wait = 0
    best_epoch = 0
    loss_logger = []
    valid_logger = []
    test_logger = []
    client_test_all = [[],[],[],[]]
    client_valid_all = [[],[],[],[]]
    communication_env_all = None
    for epoch in range(args.epochs):
        
        all_opt_and_sch = [ get_opt(args, models[idx])  for idx in range(args.client_number)]
        
    
        for client_idx, model in enumerate(models):
            for i in range(10):
                path = i % int(args.path_list[-1])
                if path in list(range(int(args.path_list[0]))):
                    optimizer_name = 'separator' 
        
                elif path in list(range(int(args.path_list[0]), int(args.path_list[1]))):
                    optimizer_name = 'predictor'


                model.train()

                optimizers,schedulers = all_opt_and_sch[client_idx]
                train_loader = partition_dicts[client_idx]['train']
                
                train_xmrl(args, model, device, train_loader, optimizers, dataset.task_type, optimizer_name,loss_logger,communication_env_all,server_model)
                
                if schedulers != None:
                    schedulers[optimizer_name].step()

        with torch.no_grad():
            bias_models = [copy.deepcopy(bias_model) for bias_model in models]
            communication_env = []
            for env_model in models:
                _, cluster_centers,_ = get_kmeans(args, env_model, device, train_loader)
                communication_env.append(cluster_centers)
            communication_env_all = torch.cat(communication_env,0)
            communication_env_all = communication_env_all.to(device)  
            

            server_model, models = communication( server_model, models, client_weights)


        server_model.eval()
        valid_perf = eval_test(args, server_model, device, valid_loader, evaluator)[0]
        
        valid_logger.append(valid_perf)
        
        update_test = False

        test_perfs = eval_test(args, server_model, device, test_loader, evaluator)
        # class_test_perfs = eval_test_class(args, server_model, device, test_loader, evaluator)
        test_auc  = test_perfs[0]
        # class_test_auc = class_test_perfs[0]
        print("=====Epoch {}, Metric: {}, Validation: {}, Test: {}, Class_Test:{}".format(epoch, 'AUC', valid_perf, test_auc,0))

        if epoch != 0:
            if 'classification' in dataset.task_type and valid_perf >  best_valid_perf:
                update_test = True
            elif 'classification' not in dataset.task_type and valid_perf <  best_valid_perf:
                update_test = True
        if update_test or epoch == 0:
            best_valid_perf = valid_perf
            test_auc1 = test_auc
            # cnt_wait = 0
            best_epoch = epoch

        else:
            # print({'Train': train_perf, 'Validation': valid_perf})
            cnt_wait += 1
            if cnt_wait > args.patience:
                break

    logger.info('Finished training! Results from epoch {} with best validation {}.'.format(best_epoch, best_valid_perf))
    print('Finished training! Results from epoch {} with best validation {}.'.format(best_epoch, best_valid_perf))
    print('bias_test')
    print(client_test_all)
    print('valid_test')
    print(client_valid_all)


    if args.dataset.startswith('ogbg'):
        logger.info('Test auc: {}'.format(test_auc))
        return [best_valid_perf, test_auc1]

    

def config_and_run(args):
    
    if args.by_default:

        if args.dataset == 'ogbg-molhiv':
            args.gamma = 0.1
            args.batch_size = 256
            args.lr = 1e-3
            args.num_layer = 4
            args.initw_name = 'orthogonal'
            args.epochs = 10
            if args.gnn == 'gcn-virtual':
                args.lr = 1e-3
                args.l2reg = 1e-5
                # args.epochs = 100
                args.num_layer = 3
                args.use_clip_norm = True
                args.path_list=[2, 4]
        if args.dataset == 'ogbg-molbace':
            args.epochs = 10
            if args.gnn == 'gin-virtual' or args.gnn == 'gin':
                # args.gnn = 'gin'
                args.l2reg = 7e-4
                args.gamma = 0.5
                args.num_layer = 4  
                args.batch_size = 256
                args.emb_dim = 128
                args.use_lr_scheduler = True
                args.patience = 100
                args.drop_ratio = 0.3
                args.initw_name = 'orthogonal' 
            if args.gnn == 'gcn-virtual' or args.gnn == 'gcn':
                # args.gnn = 'gcn'
                args.patience = 100
                args.initw_name = 'orthogonal' 
                args.num_layer = 2
                args.emb_dim = 128
                args.batch_size = 256
        if args.dataset == 'ogbg-molbbbp':
            args.l2reg = 5e-6
            args.epochs = 10
            args.initw_name = 'orthogonal'
            args.num_layer = 2
            args.emb_dim = 128
            args.batch_size = 256 
            args.use_lr_scheduler = True 
            args.gamma = 0.2
            if args.gnn == 'gcn-virtual' or args.gnn == 'gcn':
                args.gnn = 'gcn-virtual'
                args.gamma = 0.4
                args.emb_dim = 128
                args.use_lr_scheduler = False 
        if args.dataset == 'ogbg-molsider':
            if args.gnn == 'gin-virtual' or args.gnn == 'gin':
                args.gnn = 'gin'
            if args.gnn == 'gcn-virtual' or args.gnn == 'gcn':
                args.gnn = 'gcn'
            args.l2reg = 1e-4
            args.patience = 100
            args.gamma = 0.4
            args.num_layer =  5
            args.epochs = 20

        if args.dataset == 'ogbg-moltoxcast':
            if args.gnn == 'gin-virtual' or args.gnn == 'gin':  #注释掉的文件是0631ogbg-moltoxcast_gin-virtual__real_pred_rep_1.0_loss_infonce_1.0
                args.gnn = 'gin'
            if args.gnn == 'gcn-virtual' or args.gnn == 'gcn':
                args.gnn = 'gcn'
            args.patience = 50
            args.epochs = 20
            args.l2reg = 1e-5
            args.gamma = 0.4
            args.num_layer = 4

        if args.dataset == 'ogbg-molclintox':
            if args.gnn == 'gin-virtual' or args.gnn == 'gin':
                args.gnn = 'gin'
            if args.gnn == 'gcn-virtual' or args.gnn == 'gcn':
                args.gnn = 'gcn'
            args.use_linear_predictor = True
            args.use_clip_norm = True
            args.gamma = 0.2
            
            args.batch_size = 64 
            args.num_layer = 5
            args.emb_dim = 300
            args.l2reg = 1e-4
            args.epochs = 20
            args.drop_ratio=0.5
        if args.dataset == 'ogbg-moltox21':
            args.gamma = 0.3 
            args.epochs = 10



    for k, v in vars(args).items():
        logger.info("{:20} : {:10}".format(k, str(v)))

    args.plym_prop = 'none' if args.dataset.startswith('ogbg') else args.dataset.split('-')[1].split('_')[0]
    if args.dataset.startswith('ogbg'):
        results = {'valid_auc': [], 'test_auc': []}
    else:
        results = {'valid_rmse': [], 'test_rmse': [], 'test_r2':[]}
    for seed in range(args.trails):
        if args.dataset.startswith('plym'):
            valid_rmse, test_rmse, test_r2 = main(args)
            results['test_r2'].append(test_r2)
            results['test_rmse'].append(test_rmse)
            results['valid_rmse'].append(valid_rmse)
        else:
            
            valid_auc, test_auc = main(args,seed)
            results['valid_auc'].append(valid_auc)
            results['test_auc'].append(test_auc)
    for mode, nums in results.items():
        logger.info('{}: {:.4f}+-{:.4f} {}'.format(
            mode, np.mean(nums), np.std(nums), nums))
        print('{}: {:.4f}+-{:.4f} {}'.format(mode, np.mean(nums), np.std(nums), nums))


if __name__ == "__main__":
    args = get_args()
    config_and_run(args)
    




