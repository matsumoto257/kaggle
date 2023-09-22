import argparse
import pathlib
import numpy as np
import sklearn.model_selection
from torch import utils
import torch
import torchvision
import torchvision.transforms.functional
from torchvision import transforms
import os
from tqdm import tqdm



# trainデータをtrain_val_splitし、trainのインデックスとvalのインデックをそれぞれ取得
def setup_train_val_split(labels, dryrun=False, seed=0):
    x = np.arange(len(labels))
    y = np.array(labels)
    #sklearn.model_selection.StratifiedShuffleSplit
    #データセット全体のクラスの偏りを保持しながら、データセットを分割.データの重複が許容されていると分かる.必ずしも全てのデータがvalidationのデータセットに一度使われるわけではない.
    #検証用データが重複したり，ある学習用データが学習されなかったりするので，あまり使われないイメージ
    #インスタンスの作成
    splitter = sklearn.model_selection.StratifiedShuffleSplit(
        n_splits=1, 
        train_size=0.8, 
        random_state=seed    #n_splits:分割&シャッフル回数
    )
    #上で分割したtrainデータのインデックス=train_indices,valデータのインデックス=val_indices
    #x:分割するデータのインデックスの配列,y:それに対応するラベルの配列
    #next()でsplitter.split(x, y)から要素を取り出す
    train_indices, val_indices = next(splitter.split(x, y))
    #dryrun=True→ランダムに50個run
    if dryrun:
        train_indices = np.random.choice(train_indices, 50, replace=False)  #train_indicesを更新
        val_indices = np.random.choice(val_indices, 50, replace=False)

    return train_indices, val_indices


# transform
def setup_center_crop_transform():
    return transforms.Compose(
        [
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ]
    )


# trainデータの正解ラベル(dog=1,cat=0)を返す
def get_labels(dataset):
    # 結局このif文は何がしたいのか分からない、このif文はfalseになる
    if isinstance(dataset, torch.utils.data.Subset):
        return get_labels(dataset.dataset)[dataset.indices]
    else:
        #traiデータの正解ラベルを全て１次元配列で出力
        return np.array([img[1] for img in dataset.imgs])  # torchvision.datasets.ImageFolder.imgs : List of (image path, class_index) tuples


#setup_train_val_splitで分割したデータをdatasetに変換
def setup_train_val_datasets(data_dir, dryrun=False):
    #torchvision.datasets.ImageFolderを使用してデータセットの作成
    dataset = torchvision.datasets.ImageFolder(
        os.path.join(data_dir, "train"),   #trainデータの入っているディレクトリのパス
        transform=setup_center_crop_transform()   #transformを指定することによって画像に対する前処理をすることができる
    )
    # labels=正解ラベルの配列
    labels = get_labels(dataset)
    #etup_train_val_split
    train_indices, val_indices = setup_train_val_split(labels, dryrun)

    #訓練データセットと検証データセットにデータセットを分割する際はSubsetクラスを用いる
    #元のデータセットから指定したインデックスだけ使用するデータセットを作成できる.学習及びテストに使用するインデックスを予め作成しておくことで、データセットを分割できる.
    train_dataset = torch.utils.data.Subset(dataset, train_indices)
    val_dataset = torch.utils.data.Subset(dataset, val_indices)
    return train_dataset, val_dataset


#DataLoaderを設定
def setup_train_val_loaders(data_dir, batch_size, dryrun=False):
    #setup_train_val_datasetsでデータセットを分割
    train_dataset, val_dataset = setup_train_val_datasets(
        data_dir, dryrun=dryrun
    )
    #torch.utils.data.DataLoader
    #train_datasetからミニバッチごとに取り出すことを目的に使われる
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=batch_size,   #how many samples per batch to load (default: 1).
        shuffle=True,  # set to True to have the data reshuffled at every epoch (default: False).
        drop_last=True,
        num_workers=2,   #ミニバッチを作成する際の並列実行数を指定できる.最大で CPU の論理スレッド数分の高速化が期待できる.
    )
    #valデータセットからミニバッチを作成する理由がいまいち分からない
    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=batch_size,
        num_workers=2
    )
    return train_loader, val_loader


########################################################################################################################
# train loop
########################################################################################################################

#1epch train
def train_1epoch(model, train_loader, lossfun, optimizer, device):
    model.train()   #訓練モード.下で定義しているtrain()とはおそらく違う
    total_loss, total_acc = 0.0, 0.0

    for x, t in tqdm(train_loader):   #t:正解ラベル
        x = x.to(device)
        t = t.to(device)

        optimizer.zero_grad()   #累積された勾配を全て0にする.ミニバッチ毎に勾配を０に初期化
        y = model(x)    #y:予測値
        loss = lossfun(y, t)  #誤差の計算
        #Tensor.detach() は計算グラフからテンソルを切り離す関数.現在のグラフから切り離された新しいTensorを返す.
        #torch.max():この関数はTensorの要素の中で最大のものを返す.dim=1:列方向の最大値.
        _, pred = torch.max(y.detach(), 1)
        loss.backward()   #逆伝播
        optimizer.step()  #パラメータの更新

        total_loss += loss.item() * x.size(0)   #誤差を累積させる.x.size(0)を乗算する理由は分からない
        total_acc += torch.sum(pred == t)    #acc

    avg_loss = total_loss / len(train_loader.dataset)   #平均loss
    avg_acc = total_acc / len(train_loader.dataset)    #平均acc
    return avg_acc, avg_loss


#1epoch validate
def validate_1epoch(model, val_loader, lossfun, device):
    model.eval()   #評価モード、ここではパラメータの更新は行われない
    total_loss, total_acc = 0.0, 0.0

    #torch.no_grad():勾配を保持しない.テンソルの勾配の計算を不可にするContext-manager.テンソルの勾配の計算を不可にすることでメモリの消費を減らす事が出来る
    #with:__enter__()メソッドから__exit__()メソッドまでのメソッドが処理される
    with torch.no_grad():
        for x, t in tqdm(val_loader):
            x = x.to(device)
            t = t.to(device)

            y = model(x)
            loss = lossfun(y.detach(), t)
            _, pred = torch.max(y, 1)

            total_loss += loss.item() * x.size(0)
            total_acc += torch.sum(pred == t)

    avg_loss = total_loss / len(val_loader.dataset)
    avg_acc = total_acc / len(val_loader.dataset)
    return avg_acc, avg_loss


#学習したいエポック回数だけ学習
def train(model, optimizer, train_loader, val_loader, n_epochs, device):
    lossfun = torch.nn.CrossEntropyLoss()

    for epoch in tqdm(range(n_epochs)):   #学習するエポック数
        #trainのacc、loss
        train_acc, train_loss = train_1epoch(
            model, train_loader, lossfun, optimizer, device
        )
        #validateのacc、loss
        val_acc, val_loss = validate_1epoch(model, val_loader, lossfun, device)
        print(
            f"epoch={epoch}, train loss={train_loss}, train accuracy={train_acc}, val loss={val_loss}, val accuracy={val_acc}, device={device}"
        )



########################################################################################################################
# predict
########################################################################################################################

#test用DataLoaderを設定
def setup_test_loader(data_dir, batch_size, dryrun):
    dataset = torchvision.datasets.ImageFolder(
        os.path.join(data_dir, "test")   #trainデータの入っているディレクトリのパス
        , transform=setup_center_crop_transform()
    )
    #os.path.splitext() : 拡張子とそれ以外に分割されてタプルとして返される。拡張子はドット.込みの文字列。 e.g."('1', '.jpg')"
    #os.path.basename() : パス文字列からファイル名を取得する e.g."1.jpg"
    #test imageのidを取得
    image_ids = [
        os.path.splitext(os.path.basename(path))[0] for path, _ in dataset.imgs   # torchvision.datasets.ImageFolder.imgs : List of (image path, class_index) tuples
    ]

    if dryrun:
        dataset = torch.utils.data.Subset(dataset, range(0, 50))  #上から50データのデータセット
        image_ids = image_ids[:50]  #test imageのidを上から50個
    
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        num_workers=2
    )
    return loader, image_ids

#testデータに対して予測
def predict(model, loader, device):
    pred_fun = torch.nn.Softmax(dim=1)   #dim=1を指定した場合 : 行単位でSoftmaxをかけてくれる(=行の合計が1)
    preds = []

    for x, _ in tqdm(loader):
        #torch.set_grad_enabled : 勾配計算のオンまたはオフを設定するコンテキストマネージャー
        with torch.set_grad_enabled(False):
            x = x.to(device)
            y = pred_fun(model(x))
        y = y.cpu().numpy()  #TensorをNumpy Arrayに変換する.一度cpuに移してからnumpy arrayに変換
        y = y[:,1]   # cat:0, dog: 1
        preds.append(y)
    preds = np.concatenate(preds)
    return preds

#out_path下のファイルにtest idとその予測値を書き込む
def write_prediction(image_ids, prediction, out_path):
    with open(out_path, "w") as f:
        f.write("id, label\n")   #open().write() : 文字列"id, label"を書き込み、\n : 改行
        for i, p in zip(image_ids, prediction):   #zip : 複数のリストの要素をまとめて取得
            f.write("{},{}\n".format(i, p))



########################################################################################################################
# 各種実行設定
########################################################################################################################

#
# 5: First try
#

#1エポック(=625イテレーション|batch_size = 32)の学習を実行
def train_subsec5(data_dir, batch_size, dryrun=False, device="cuda:0"):
    model = torchvision.models.resnet50(weights=torchvision.models.ResNet50_Weights.IMAGENET1K_V2)    #事前学習済みresnet50
    model.fc = torch.nn.Linear(model.fc.in_features, 2)   #出力層が1000次元になっているため2クラス分類に合わせる
    model.to(device)

    optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)   #最適化アルゴリズム:SGD.momentumが分からない
    #DataLoaderを呼び出す
    train_loader, val_loader = setup_train_val_loaders(
        data_dir, batch_size, dryrun
    )
    train(
        model, optimizer, train_loader, val_loader, n_epochs=1, device=device
    )
    return model


#testデータに対する予測、出力を実行
def predict_subsec5(
        data_dir, out_dir, model, batch_size, dryrun=False, device="cuda:0"
):
    test_loader, image_ids = setup_test_loader(
        data_dir, batch_size, dryrun=dryrun
    )
    preds = predict(model, test_loader, device)
    write_prediction(image_ids, preds, out_dir / "out.csv")  #Pathオブジェクトに対して/演算子を使うとパスが連結される


#学習から推論まで一連をまとめて実行(base model)
def run_5(data_dir, out_dir, dryrun, device):
    batch_size = 32
    model = train_subsec5(data_dir, batch_size, dryrun, device)

    # clip無しの推論
    predict_subsec5(data_dir, out_dir, model, batch_size, dryrun, device)



def main():
    parser = argparse.ArgumentParser()   #パーサを作る
    # parser.add_argumentで受け取る引数を追加していく
    parser.add_argument("--data_dir", required=True)  # オプション引数を追加,required=True:指定必須
    parser.add_argument("--out_dir", default="./out")
    parser.add_argument("--forecasts", action="store_true")  #学習のみか学習&推論 true : 推論も
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dryrun", action="store_true")   #オプションを指定:True、オプションを指定しない:False
    args = parser.parse_args()  # 引数を解析

    #引数をオブジェクトに
    data_dir = pathlib.Path(args.data_dir)  #データのあるパス
    out_dir = pathlib.Path(args.out_dir)  #予測結果の出力先のディレクトリのパス
    out_dir.mkdir(parents=True, exist_ok=True)   #Pathオブジェクト.makdir() : ディレクトリ作成
    forecasts = args.forecasts
    device = args.device
    dryrun = args.dryrun

    train_dir = os.path.join(data_dir, "train")

    print(data_dir)   #パスオブジェクト
    print(type(data_dir))
    print(data_dir.exists())    #パスの存在を確認
    print(train_dir)
    print(device)
    if torch.cuda.is_available():
        print('cuda:0 is available')
    else:
        device = "cpu"
        print('cuda:0 is not available')


    #学習のみ
    if forecasts == False:
        batch_size = 32
        train_subsec5(data_dir=data_dir, batch_size=batch_size, dryrun=dryrun, device=device)
    #学習、推論
    elif forecasts:
        run_5(data_dir, out_dir, dryrun, device)

if __name__ == "__main__":
    main()
