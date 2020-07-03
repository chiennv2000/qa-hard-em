# Copyright 2019-present NAVER Corp.
# Apache License v2.0

# Wonseok Hwang
# Sep30, 2018
import os, sys, argparse, re, json
from tqdm import tqdm

from matplotlib.pylab import *
import torch.nn as nn
import torch
import torch.nn.functional as F
import random as python_random
# import torchvision.datasets as dsets

# BERT
import bert.tokenization as tokenization
from bert.modeling import BertConfig, BertModel

from sqlova.utils.utils_wikisql import *
from sqlova.model.nl2sql.wikisql_models import *
from sqlnet.dbengine import DBEngine

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def construct_hyper_param(parser):
    parser.add_argument('--tepoch', default=200, type=int)
    parser.add_argument("--bS", default=32, type=int,
                        help="Batch size")
    parser.add_argument("--accumulate_gradients", default=1, type=int,
                        help="The number of accumulation of backpropagation to effectivly increase the batch size.")
    parser.add_argument('--fine_tune',
                        default=False,
                        action='store_true',
                        help="If present, BERT is trained.")
    parser.add_argument("--path_wikisql", default='wikisql', type=str,
                        help="data directory.")
    parser.add_argument("--path_out", type=str,
                        help="out directory.")
    parser.add_argument("--model_type", default='Seq2SQL_v1', type=str,
                        help="Type of model.")
    parser.add_argument("--loss_type", default="sum", type=str)
    parser.add_argument("--debug", action="store_true",
                        help="debug mode.")
    parser.add_argument("--trained", action="store_true", help="restored mode.")
    parser.add_argument("--version", default=None, type=str)
    parser.add_argument("--test", type=str, default=None, help="test mode.")

    # 1.2 BERT Parameters
    parser.add_argument("--vocab_file",
                        default='vocab.txt', type=str,
                        help="The vocabulary file that the BERT model was trained on.")
    parser.add_argument("--max_seq_length",
                        default=180, type=int, # Set based on maximum length of input tokens.
                        help="The maximum total input sequence length after WordPiece tokenization. Sequences "
                             "longer than this will be truncated, and sequences shorter than this will be padded.")
    parser.add_argument("--num_target_layers",
                        default=2, type=int,
                        help="The Number of final layers of BERT to be used in downstream task.")
    parser.add_argument('--lr_bert', default=1e-5, type=float, help='BERT model learning rate.')
    parser.add_argument('--seed', type=int, default=1,
                        help="random seed for initialization")
    parser.add_argument('--no_pretraining', action='store_true', help='Use BERT pretrained model')
    parser.add_argument("--bert_type_abb", default='uS', type=str,
                        help="Type of BERT model to load. e.g.) uS, uL, cS, cL, and mcS")

    # 1.3 Seq-to-SQL module parameters
    parser.add_argument('--lS', default=2, type=int, help="The number of LSTM layers.")
    parser.add_argument('--dr', default=0.3, type=float, help="Dropout rate.")
    parser.add_argument('--lr', default=0.001, type=float, help="Learning rate.")
    parser.add_argument("--hS", default=100, type=int, help="The dimension of hidden vector in the seq-to-SQL module.")

    # 1.4 Execution-guided decoding beam-size. It is used only in test.py
    parser.add_argument('--EG',
                        default=False,
                        action='store_true',
                        help="If present, Execution guided decoding is used in test.")
    parser.add_argument('--beam_size',
                        type=int,
                        default=4,
                        help="The size of beam for smart decoding")

    args = parser.parse_args()

    map_bert_type_abb = {'uS': 'uncased_L-12_H-768_A-12',
                         'uL': 'uncased_L-24_H-1024_A-16',
                         'cS': 'cased_L-12_H-768_A-12',
                         'cL': 'cased_L-24_H-1024_A-16',
                         'mcS': 'multi_cased_L-12_H-768_A-12'}
    args.bert_type = map_bert_type_abb[args.bert_type_abb]

    # Decide whether to use lower_case.
    if args.bert_type_abb == 'cS' or args.bert_type_abb == 'cL' or args.bert_type_abb == 'mcS':
        args.do_lower_case = False
    else:
        args.do_lower_case = True

    # Seeds for random number generation
    seed(args.seed)
    python_random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available:
        torch.cuda.manual_seed_all(args.seed)

    #args.toy_model = not torch.cuda.is_available()
    args.toy_model = False
    args.toy_size = 12

    return args


def get_bert(BERT_PT_PATH, bert_type, do_lower_case, no_pretraining):


    bert_config_file = os.path.join(BERT_PT_PATH, bert_type, 'bert_config.json')
    vocab_file = os.path.join(BERT_PT_PATH, bert_type, 'vocab.txt')
    init_checkpoint = os.path.join(BERT_PT_PATH, bert_type, 'pytorch_model.bin')

    bert_config = BertConfig.from_json_file(bert_config_file)
    tokenizer = tokenization.FullTokenizer(
        vocab_file=vocab_file, do_lower_case=do_lower_case)
    bert_config.print_status()

    model_bert = BertModel(bert_config)
    if no_pretraining:
        pass
    else:
        model_bert.load_state_dict(torch.load(init_checkpoint, map_location='cpu'))
        print("Load pre-trained parameters.")
    model_bert.to(device)

    return model_bert, tokenizer, bert_config

def get_opt(model, model_bert, fine_tune):
    if fine_tune:
        opt = torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),
                               lr=args.lr, weight_decay=0)

        opt_bert = torch.optim.Adam(filter(lambda p: p.requires_grad, model_bert.parameters()),
                                    lr=args.lr_bert, weight_decay=0)
    else:
        opt = torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),
                               lr=args.lr, weight_decay=0)
        opt_bert = None

    return opt, opt_bert

def get_models(args, BERT_PT_PATH, trained=False, path_model_bert=None, path_model=None):
    # some constants
    agg_ops = ['', 'MAX', 'MIN', 'COUNT', 'SUM', 'AVG']
    cond_ops = ['=', '>', '<', 'OP']  # do not know why 'OP' required. Hence,

    print("Batch_size = {args.bS * args.accumulate_gradients}")
    print("BERT parameters:")
    print("learning rate: {args.lr_bert}")
    print("Fine-tune BERT: {args.fine_tune}")

    # Get BERT
    model_bert, tokenizer, bert_config = get_bert(BERT_PT_PATH, args.bert_type, args.do_lower_case,
                                                  args.no_pretraining)
    args.iS = bert_config.hidden_size * args.num_target_layers  # Seq-to-SQL input vector dimenstion

    # Get Seq-to-SQL

    n_cond_ops = len(cond_ops)
    n_agg_ops = len(agg_ops)
    print("Seq-to-SQL: the number of final BERT layers to be used: {args.num_target_layers}")
    print("Seq-to-SQL: the size of hidden dimension = {args.hS}")
    print("Seq-to-SQL: LSTM encoding layer size = {args.lS}")
    print("Seq-to-SQL: dropout rate = {args.dr}")
    print("Seq-to-SQL: learning rate = {args.lr}")
    model = Seq2SQL_v1(args.iS, args.hS, args.lS, args.dr, n_cond_ops, n_agg_ops)
    model = model.to(device)

    if args.trained:
        path_model_bert = os.path.join('out', args.path_out, 'model_bert_best.pt')
        path_model = os.path.join('out', args.path_out, 'model_best.pt')


        if torch.cuda.is_available():
            res = torch.load(path_model_bert)
        else:
            res = torch.load(path_model_bert, map_location='cpu')
        model_bert.load_state_dict(res['model_bert'])
        model_bert.to(device)

        if torch.cuda.is_available():
            res = torch.load(path_model)
        else:
            res = torch.load(path_model, map_location='cpu')

        model.load_state_dict(res['model'])

    return model, model_bert, tokenizer, bert_config

def get_data(path_wikisql, args):
    train_data, train_table, dev_data, dev_table, _, _ = load_wikisql(args, path_wikisql, args.version, args.toy_model, args.toy_size, no_w2i=True, no_hs_tok=True)
    if args.debug:
        train_data = train_data[:10000]
        dev_data = dev_data[:5000]
    print ("{} train data, {} dev data".format(len(train_data), len(dev_data)))
    print (len(train_table), len(dev_table))
    train_loader, dev_loader = get_loader_wikisql(train_data, dev_data, args.bS,
                                                  shuffle_train=True)

    return train_data, train_table, dev_data, dev_table, train_loader, dev_loader


def train(args, train_loader, dev_loader, train_table, dev_table, model, model_bert, opt, bert_config, tokenizer,
          max_seq_length, num_target_layers, accumulate_gradients=1, check_grad=True,
          st_pos=0, opt_bert=None, path_db=None, dset_name='train',
          path_save_for_evaluation="pred"):

    acc_lx_t_best = -1
    epoch_best = -1
    model.train()
    model_bert.train()

    global_step = 0

    for epoch in range(args.tepoch):

        ave_loss = 0
        cnt = 0 # count the # of examples
        cnt_sc = 0 # count the # of correct predictions of select column
        cnt_sa = 0 # of selectd aggregation
        cnt_wn = 0 # of where number
        cnt_wc = 0 # of where column
        cnt_wo = 0 # of where operator
        cnt_wv = 0 # of where-value
        cnt_wvi = 0 # of where-value index (on question tokens)
        cnt_lx = 0  # of logical form acc
        #cnt_x = 0   # of execution acc

        print ("Starting epoch={}".format(epoch))
        for iB, t in tqdm(enumerate(train_loader)):
            global_step += 1
            cnt += len(t)

            if cnt < st_pos:
                continue

            def _get_loss(t):
                # Get fields
                nlu, nlu_t, sql_i, sql_q, sql_t, tb, hs_t, hds, g_wvi_corenlp = get_fields(\
                                                t, train_table, train=True, no_hs_t=True, no_sql_t=True)
                # nlu  : natural language utterance
                # nlu_t: tokenized nlu
                # sql_i: canonical form of SQL query
                # sql_q: full SQL query text. Not used.
                # sql_t: tokenized SQL query
                # tb   : table
                # hs_t : tokenized headers. Not used.

                g_sc, g_sa, g_wn, g_wc, g_wo, g_wv = get_g(sql_i)
                # get ground truth where-value index under CoreNLP tokenization scheme. It's done already on trainset.
                #g_wvi_corenlp = get_g_wvi_corenlp(t)

                wemb_n, wemb_h, l_n, l_hpu, l_hs, \
                        nlu_tt, t_to_tt_idx, tt_to_t_idx \
                        = get_wemb_bert(bert_config, model_bert, tokenizer, nlu_t, hds, \
                                        max_seq_length,
                                        num_out_layers_n=num_target_layers,
                                        num_out_layers_h=num_target_layers)
                # wemb_n: natural language embedding
                # wemb_h: header embedding
                # l_n: token lengths of each question
                # l_hpu: header token lengths
                # l_hs: the number of columns (headers) of the tables.

                try:
                    #
                    g_wvi = get_g_wvi_bert_from_g_wvi_corenlp(t_to_tt_idx, g_wvi_corenlp)
                except:
                    # Exception happens when where-condition is not found in nlu_tt.
                    # In this case, that train example is not used.
                    # During test, that example considered as wrongly answered.
                    # e.g. train: 32.
                    print ("Error due to non-span phrase")
                    return None
                wemb_n_, l_n_, wemb_h_, l_hpu_, l_hs_ = [], [], [], [], []
                offset = 0
                for g_i, g in enumerate(g_sc):
                    wemb_n_ += [wemb_n[g_i:g_i+1,:,:] for _ in range(len(g))]
                    l_n_ += [l_n[g_i] for _ in range(len(g))]
                    wemb_h_ += [wemb_h[offset:offset+l_hs[g_i]] for _ in range(len(g))]
                    for _ in range(len(g)):
                        l_hpu_ += l_hpu[offset:offset+l_hs[g_i]]
                    l_hs_ += [l_hs[g_i] for _ in range(len(g))]
                    offset += l_hs[g_i]
                #loss  = losses
                wemb_n = torch.cat(wemb_n_, dim=0).contiguous()
                wemb_h = torch.cat(wemb_h_, dim=0).contiguous()
                l_n, l_hpu, l_hs = l_n_, l_hpu_, l_hs_

                Z = (wemb_h.size(0) *  wemb_h.size(1))
                if Z > 100000:
                    print ("Error due to too huge wemb_h")
                    return None

                # sel, agg, num of conds, header of conds, operation of conds, spans of conds
                g_sc_, g_sa_, g_wn_, g_wc_, g_wvi_, g_wo_ = [i for g in g_sc for i in g], \
                                                    [i for g in g_sa for i in g], \
                                                    [i for g in g_wn for i in g], \
                                                    [i for g in g_wc for i in g], \
                                                    [i for g in g_wvi for i in g], \
                                                    [i for g in g_wo for i in g]
                for i in range(len(g_wn_)):
                    if g_wn_[i] != len(g_wvi_[i]):
                        print ("span mismatch")
                        from IPython import embed; embed()
                        exit()

                # score
                for (l_ni, sci, wci, wvi, l_hsi) in zip(l_n, g_sc_, g_wc_, g_wvi_, l_hs):
                    try:
                        assert sci<l_hsi #all([scij<l_hsi for scij in sci])
                        assert len(wvi)==0 or np.max(wvi)<l_ni
                        assert len(wci)==0 or np.max(wci)<l_hsi #all([max(wcij)<l_hsi for wcij in wci])
                    except Exception:
                        print ("Error due to truncating the input. Skip this batch")
                        #from IPython import embed; embed()
                        #exit()
                        return None

                s_sc, s_sa, s_wn, s_wc, s_wo, s_wv = model(wemb_n, l_n, wemb_h, l_hpu, l_hs,
                                                        g_sc=g_sc_, g_sa=g_sa_,
                                                        g_wn=g_wn_, g_wc=g_wc_, g_wvi=g_wvi_)
                #except Exception:
                #    print ("Error in line 324")
                #    from IPython import embed; embed()
                #    exit()

                # Calculate loss & step
                losses = Loss_sw_se(s_sc, s_sa, s_wn, s_wc, s_wo, s_wv,
                                g_sc_, g_sa_, g_wn_, g_wc_, g_wo_, g_wvi_, reduction='none')
                loss = 0
                offset = 0
                for g_i, g in enumerate(g_sc):
                    if len(g)==0: continue
                    if args.loss_type == 'sum':
                        loss += torch.log(torch.sum(torch.exp(losses[offset:offset+len(g)])))
                    elif args.loss_type == 'max':
                        loss += torch.min(losses[offset:offset+len(g)])
                    elif args.loss_type == 'top3':
                        losses_top3 = torch.topk(losses[offset:offset+len(g)],
                                                 k=min(len(g),3), largest=False)[0]
                        loss += torch.log(torch.sum(torch.exp(losses_top3)))
                    else:
                        raise NotImplementedError()
                    offset += len(g)

                #if loss is None:
                #    continue
                del losses
                try:
                    loss.backward()
                    opt.step()
                    model.zero_grad()
                    if opt_bert:
                        opt_bert.step()
                        model_bert.zero_grad()
                    del loss
                except Exception:
                    print ("Memory error!", Z)
                    del loss
                    torch.cuda.empty_cache()

            if global_step <= 7800: continue
            _get_loss(t)

            if global_step % 10 == 0:
                torch.cuda.empty_cache()

            if epoch>0 and global_step % 200 == 0:
                model.eval()
                model_bert.eval()
                with torch.no_grad():
                    acc_dev, results_dev, cnt_list = test(args, dev_loader, dev_table, model, model_bert,
                                                bert_config, tokenizer, args.max_seq_length,
                                                args.num_target_layers, detail=False,
                                                path_db=path_wikisql, st_pos=0, dset_name='dev', EG=args.EG)
                print_result(global_step, acc_dev, 'dev')
                save_for_evaluation(path_save_for_evaluation, results_dev, 'dev_{}'.format(global_step))

                acc_lx_t = acc_dev[-2]
                if acc_lx_t > acc_lx_t_best:
                    acc_lx_t_best = acc_lx_t
                    epoch_best = epoch
                    # save best model
                    state = {'model': model.state_dict()}
                    torch.save(state, os.path.join('out', args.path_out, 'model_best.pt') )

                    state = {'model_bert': model_bert.state_dict()}
                    torch.save(state, os.path.join('out', args.path_out, 'model_bert_best.pt'))

                print(" Best Dev lx acc: {} at epoch: {}".format(acc_lx_t_best, epoch_best))
                model.train()
                model_bert.train()

    ave_loss /= cnt
    acc_sc = cnt_sc / cnt
    acc_sa = cnt_sa / cnt
    acc_wn = cnt_wn / cnt
    acc_wc = cnt_wc / cnt
    acc_wo = cnt_wo / cnt
    acc_wvi = cnt_wv / cnt
    acc_wv = cnt_wv / cnt
    acc_lx = cnt_lx / cnt
    acc_x = 0 #cnt_x / cnt

    acc = [ave_loss, acc_sc, acc_sa, acc_wn, acc_wc, acc_wo, acc_wvi, acc_wv, acc_lx, acc_x]

    aux_out = 1

    return acc, aux_out

def report_detail(hds, nlu,
                  g_sc, g_sa, g_wn, g_wc, g_wo, g_wv, g_wv_str, g_sql_q, g_ans,
                  pr_sc, pr_sa, pr_wn, pr_wc, pr_wo, pr_wv_str, pr_sql_q, pr_ans,
                  cnt_list, current_cnt):
    cnt_tot, cnt, cnt_sc, cnt_sa, cnt_wn, cnt_wc, cnt_wo, cnt_wv, cnt_wvi, cnt_lx, cnt_x = current_cnt

    print('cnt = {} / {} ==============================='.format(cnt, cnt_tot))

    print('headers: {}'.format(hds))
    print('nlu: {}'.format(nlu))

    # print(f's_sc: {s_sc[0]}')
    # print(f's_sa: {s_sa[0]}')
    # print(f's_wn: {s_wn[0]}')
    # print(f's_wc: {s_wc[0]}')
    # print(f's_wo: {s_wo[0]}')
    # print(f's_wv: {s_wv[0][0]}')
    print('===============================')
    print('g_sc : {}'.format(g_sc))
    print('pr_sc: {pr_sc}'.format(pr_sc))
    print('g_sa : {g_sa}'.format(g_sa))
    print('pr_sa: {pr_sa}'.format(pr_sa))
    print('g_wn : {g_wn}'.format(g_wn))
    print('pr_wn: {pr_wn}'.format(pr_wn))
    print('g_wc : {g_wc}'.format(g_wc))
    print('pr_wc: {pr_wc}'.format(pr_wc))
    print('g_wo : {g_wo}'.formrat(g_wo))
    print('pr_wo: {pr_wo}'.format(pr_wo))
    print('g_wv : {g_wv}'.format(g_wv))
    # print(f'pr_wvi: {pr_wvi}')
    print('g_wv_str:', g_wv_str)
    print('p_wv_str:', pr_wv_str)
    print('g_sql_q:  {}'.format(g_sql_q))
    print('pr_sql_q: {}'.format(pr_sql_q))
    #print('g_ans: {g_ans}')
    #print('pr_ans: {pr_ans}')
    print('--------------------------------')

    print(cnt_list)

    cnt = 1.0 * cnt
    #print('acc_lx = {cnt_lx/cnt:.3f}, acc_x = {cnt_x/cnt:.3f}\n',
    #      'acc_sc = {cnt_sc/cnt:.3f}, acc_sa = {cnt_sa/cnt:.3f}, acc_wn = {cnt_wn/cnt:.3f}\n',
    #      'acc_wc = {cnt_wc/cnt:.3f}, acc_wo = {cnt_wo/cnt:.3f}, acc_wv = {cnt_wv/cnt:.3f}')
    print ('acc_lx = %.3f, acc_x = %.3f, acc_sc = %.3f, acc_sa = %.3f, acc_wn = %.3f' % (\
                    cnt_lx/cnt, cnt_x/cnt, cnt_sc/cnt, cnt_sa/cnt, cnt_wn/cnt))
    print ('acc_wc = %.3f, acc_wo = %.3f, acc_wv = %.3f' % (cnt_wc/cnt, cnt_wo/cnt, cnt_wv/cnt))
    print('===============================')

def test(args, data_loader, data_table, model, model_bert, bert_config, tokenizer,
         max_seq_length,
         num_target_layers, detail=False, st_pos=0, cnt_tot=1, EG=False, beam_size=4,
         path_db=None, dset_name='test'):
    model.eval()
    model_bert.eval()

    ave_loss = 0
    cnt = 0
    cnt_sc = 0
    cnt_sa = 0
    cnt_wn = 0
    cnt_wc = 0
    cnt_wo = 0
    cnt_wv = 0
    cnt_wvi = 0
    cnt_lx = 0
    cnt_x = 0

    cnt_list = []

    engine = DBEngine(os.path.join(path_db, "{dset_name}.db"))
    results = []
    for iB, t in enumerate(data_loader):

        cnt += len(t)
        if cnt < st_pos:
            continue

        def _get_result(t):
            results = []
            # Get fields
            nlu, nlu_t, sql_i, sql_q, sql_t, tb, hs_t, hds = get_fields(t, data_table, no_hs_t=True, no_sql_t=True)

            g_sc, g_sa, g_wn, g_wc, g_wo, g_wv = get_g(sql_i)
            g_wvi_corenlp = get_g_wvi_corenlp(t)

            g_sc_, g_sa_, g_wn_, g_wc_, g_wvi_corenlp_, g_wo_, sql_i_ = [i for g in g_sc for i in g], \
                                                [i for g in g_sa for i in g], \
                                                [i for g in g_wn for i in g], \
                                                [i for g in g_wc for i in g], \
                                                [i for g in g_wvi_corenlp for i in g], \
                                                [i for g in g_wo for i in g], \
                                                [i for g in sql_i for i in g]
            wemb_n, wemb_h, l_n, l_hpu, l_hs, \
            nlu_tt, t_to_tt_idx, tt_to_t_idx \
                = get_wemb_bert(bert_config, model_bert, tokenizer, nlu_t, hds, max_seq_length,
                                num_out_layers_n=num_target_layers, num_out_layers_h=num_target_layers)

            tt_to_t_idx_, nlu_t_, nlu_tt_, nlu_, tb_ = [], [], [], [], []
            offset = 0
            for g_i, g in enumerate(g_sc):
                tt_to_t_idx_ += [tt_to_t_idx[g_i] for _ in range(len(g))]
                nlu_t_ += [nlu_t[g_i]  for _ in range(len(g))]
                nlu_tt_ += [nlu_tt[g_i]  for _ in range(len(g))]
                nlu_ += [nlu[g_i]  for _ in range(len(g))]
                tb_ += [tb[g_i] for _ in range(len(g))]
                offset += l_hs[g_i]

            if not args.test:
                try:
                    g_wvi = get_g_wvi_bert_from_g_wvi_corenlp(t_to_tt_idx, g_wvi_corenlp)
                    g_wvi_ = [i for g in g_wvi for i in g]
                    g_wv_str, g_wv_str_wp = convert_pr_wvi_to_string(g_wvi_, nlu_t_, nlu_tt_,
                                                                    tt_to_t_idx_, nlu_)

                except:
                    # Exception happens when where-condition is not found in nlu_tt.
                    # In this case, that train example is not used.
                    # During test, that example considered as wrongly answered.
                    print ("Except!!! in line 555")
                    from IPython import embed; embed()
                    exit()
                    for b in range(len(nlu)):
                        results1 = {}
                        results1["error"] = "Skip happened"
                        results1["nlu"] = nlu[b]
                        results1["table_id"] = tb[b]["id"]
                        results.append(results1)
                    return None, results

            # model specific part
            # score
            if not EG:
                # No Execution guided decoding
                s_sc, s_sa, s_wn, s_wc, s_wo, s_wv = model(wemb_n, l_n, wemb_h, l_hpu, l_hs)

                # get loss & step
                '''
                losses = Loss_sw_se(s_sc, s_sa, s_wn, s_wc, s_wo, s_wv,
                                g_sc_, g_sa_, g_wn_, g_wc_, g_wo_, g_wvi_, reduction='none')
                loss, offset = 0, 0
                for g_i, g in enumerate(g_sc):
                    if args.loss_type == 'sum':
                        loss += torch.sum(losses[offset:offset+len(g)])
                    elif args.loss_type == 'max':
                        loss += torch.min(losses[offset:offset+len(g)])
                    else:
                        raise NotImplementedError()
                    offset += len(g)
                '''
                # prediction
                pr_sc, pr_sa, pr_wn, pr_wc, pr_wo, pr_wvi = pred_sw_se(s_sc, s_sa, s_wn, s_wc, s_wo, s_wv)
                pr_wv_str, pr_wv_str_wp = convert_pr_wvi_to_string(pr_wvi, nlu_t, nlu_tt,
                                                                tt_to_t_idx, nlu)
                # g_sql_i = generate_sql_i(g_sc, g_sa, g_wn, g_wc, g_wo, g_wv_str, nlu)
                pr_sql_i = generate_sql_i(pr_sc, pr_sa, pr_wn, pr_wc, pr_wo, pr_wv_str, nlu)
            else:
                # Execution guided decoding
                prob_sca, prob_w, prob_wn_w, pr_sc, pr_sa, pr_wn, pr_sql_i = \
                    model.beam_forward(wemb_n, l_n, wemb_h, l_hpu,
                                    l_hs, engine, tb, tt_to_t_idx, nlu,
                                    beam_size=beam_size)
                # sort and generate
                pr_wc, pr_wo, pr_wv, pr_sql_i = sort_and_generate_pr_w(pr_sql_i)

                # Follosing variables are just for the consistency with no-EG case.
                pr_wvi = None # not used
                pr_wv_str=None
                pr_wv_str_wp=None
                loss = torch.tensor([0])

            g_sql_q = generate_sql_q(sql_i_, tb_)
            pr_sql_q = generate_sql_q(pr_sql_i, tb)

            # Saving for the official evaluation later.
            for b, pr_sql_i1 in enumerate(pr_sql_i):
                results1 = {}
                results1["query"] = pr_sql_i1
                #results1["table_id"] = tb_[b]["id"]
                #results1["nlu"] = nlu_[b]
                results.append(results1)

            if args.test:
                return None, results  #FIXME

            pr_sc_, pr_sa_, pr_wn_, pr_wc_, pr_wo_, pr_wvi_, pr_sql_i_ =  [],  [], [], [], [], [], []
            for g_i, g in enumerate(g_sc):
                pr_sc_ +=  [pr_sc[g_i] for  _ in range(len(g))]
                pr_sa_ +=  [pr_sa[g_i] for  _ in range(len(g))]
                pr_wn_ +=  [pr_wn[g_i] for  _ in range(len(g))]
                pr_wc_ +=  [pr_wc[g_i] for  _ in range(len(g))]
                pr_wo_ +=  [pr_wo[g_i] for  _ in range(len(g))]
                pr_wvi_ +=  [pr_wvi[g_i] for  _ in range(len(g))]
                pr_sql_i_ +=  [pr_sql_i[g_i] for  _ in range(len(g))]

            cnt_sc1_list, cnt_sa1_list, cnt_wn1_list, \
            cnt_wc1_list, cnt_wo1_list, \
            cnt_wvi1_list, cnt_wv1_list = get_cnt_sw_list(
                                                g_sc_, g_sa_, g_wn_, g_wc_ ,g_wo_, g_wvi_,
                                                pr_sc_, pr_sa_, pr_wn_, pr_wc_, pr_wo_, pr_wvi_,
                                                sql_i_, pr_sql_i_, mode='test')

            cnt_lx1_list = get_cnt_lx_list(cnt_sc1_list, cnt_sa1_list, cnt_wn1_list, cnt_wc1_list,
                                        cnt_wo1_list, cnt_wv1_list)

            cnt_sc1_list_, cnt_sa1_list_, cnt_wn1_list_, cnt_wc1_list_, cnt_wo1_list_, \
                cnt_wvi1_list_, cnt_wv1_list_, cnt_lx1_list_ = [], [], [], [], [], [], [], []

            offset = 0
            for g_i, g in enumerate(g_sc):
                if len(g)==0:
                    continue
                index = offset + np.argmax(cnt_lx1_list[offset:offset+len(g)])
                cnt_sc1_list_.append(cnt_sc1_list[index])
                cnt_sa1_list_.append(cnt_sa1_list[index])
                cnt_wn1_list_.append(cnt_wn1_list[index])
                cnt_wc1_list_.append(cnt_wc1_list[index])
                cnt_wo1_list_.append(cnt_wo1_list[index])
                cnt_wvi1_list_.append(cnt_wvi1_list[index])
                cnt_wv1_list_.append(cnt_wv1_list[index])
                cnt_lx1_list_.append(cnt_lx1_list[index])

            return [sum(x) for x in [cnt_sc1_list_, cnt_sa1_list_, cnt_wn1_list_, cnt_wc1_list_, \
                                     cnt_wo1_list_, cnt_wv1_list_, cnt_wvi1_list_, cnt_lx1_list_]], results

            # Execution accura y test
            #cnt_x1_list = []
            # lx stands for logical form accuracy

            # Execution accuracy test.
            #cnt_x1_list, g_ans, pr_ans = get_cnt_x_list(engine, tb, g_sc, g_sa, sql_i, pr_sc, pr_sa, pr_sql_i)

        curr, curr_results = _get_result(t)
        results += curr_results
        # stat
        ave_loss += 0 #loss.item()

        # count
        if curr is not None:
            cnt_sc += curr[0]
            cnt_sa += curr[1]
            cnt_wn += curr[2]
            cnt_wc += curr[3]
            cnt_wo += curr[4]
            cnt_wv += curr[5]
            cnt_wvi += curr[6]
            cnt_lx += curr[7]
            cnt_x += 0 #sum(cnt_x1_list)

    ave_loss /= cnt
    acc_sc = cnt_sc / cnt
    acc_sa = cnt_sa / cnt
    acc_wn = cnt_wn / cnt
    acc_wc = cnt_wc / cnt
    acc_wo = cnt_wo / cnt
    acc_wvi = cnt_wvi / cnt
    acc_wv = cnt_wv / cnt
    acc_lx = cnt_lx / cnt
    acc_x = cnt_x / cnt

    acc = [ave_loss, acc_sc, acc_sa, acc_wn, acc_wc, acc_wo, acc_wvi, acc_wv, acc_lx, acc_x]
    return acc, results, cnt_list


def print_result(epoch, acc, dname):
    ave_loss, acc_sc, acc_sa, acc_wn, acc_wc, acc_wo, acc_wvi, acc_wv, acc_lx, acc_x = acc

    print('{} results ------------'.format(dname))
    print(" Epoch: %d, ave loss: %.3f, acc_sc: %.3f, acc_sa: %.3f, acc_wn: %.3f, acc_wc: %.3f \
        \nacc_wo: %.3f, acc_wvi: %.3f, acc_wv: %.3f, acc_lx: %.3f, " % ( \
                        epoch, ave_loss, acc_sc, acc_sa, acc_wn, acc_wc, acc_wo, \
                        acc_wvi, acc_wv, acc_lx))

if __name__ == '__main__':

    ## 1. Hyper parameters
    parser = argparse.ArgumentParser()
    args = construct_hyper_param(parser)

    ## 2. Paths
    path_wikisql = args.path_wikisql
    BERT_PT_PATH = '' #path_wikisql

    path_save_for_evaluation = os.path.join('out', args.path_out)
    if not os.path.exists(path_save_for_evaluation):
        os.mkdir(path_save_for_evaluation)

    ## 3. Load data
    train_data, train_table, dev_data, dev_table, train_loader, dev_loader = get_data(path_wikisql, args)
    # test_data, test_table = load_wikisql_data(path_wikisql, mode='test', toy_model=args.toy_model, toy_size=args.toy_size, no_hs_tok=True)
    # test_loader = torch.utils.data.DataLoader(
    #     batch_size=args.bS,
    #     dataset=test_data,
    #     shuffle=False,
    #     num_workers=4,
    #     collate_fn=lambda x: x  # now dictionary values are not merged!
    # )
    ## 4. Build & Load models
    model, model_bert, tokenizer, bert_config = get_models(args, BERT_PT_PATH)
    ## 4.1.
    # To start from the pre-trained models, un-comment following lines.
    # path_model_bert =
    # path_model =
    # model, model_bert, tokenizer, bert_config = get_models(args, BERT_PT_PATH, trained=True, path_model_bert=path_model_bert, path_model=path_model)


    ## 5. if args.test, then just test and finish
    if args.test is not None:
        model.eval()
        model_bert.eval()
        with torch.no_grad():
            acc_dev, results_dev, cnt_list = test(args, dev_loader, dev_table, model, model_bert,
                                        bert_config, tokenizer, args.max_seq_length,
                                        args.num_target_layers, detail=False,
                                        path_db=path_wikisql, st_pos=0, dset_name=args.test,
                                        EG=args.EG)
        print_result(-1, acc_dev, args.test)
        save_for_evaluation(path_save_for_evaluation, results_dev, '{}_final'.format(args.test))
        exit()


    ## 6. Get optimizers
    opt, opt_bert = get_opt(model, model_bert, args.fine_tune)

    ## 7. Train
    train(args, train_loader, dev_loader,
                                         train_table,  dev_table,
                                         model,
                                         model_bert,
                                         opt,
                                         bert_config,
                                         tokenizer,
                                         args.max_seq_length,
                                         args.num_target_layers,
                                         args.accumulate_gradients,
                                         opt_bert=opt_bert,
                                         st_pos=0,
                                         path_db=os.path.join(path_wikisql, 'data'),
                                         dset_name='train',
                                        path_save_for_evaluation=path_save_for_evaluation)

