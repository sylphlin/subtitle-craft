"""
Google Cloud Speech-to-Text V2 (Chirp 3) Acoustic Word-Timestamp Module (chirp3_asr.py).
Provides the default Stage 2 cloud ASR engine for Subtitle Craft.
Uses Application Default Credentials (ADC) and GCS BatchRecognize with enableWordTimeOffsets=True.
"""

import concurrent.futures
import json
import os
import random
import re
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import uuid

import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest

from .gcp_client import ensure_gcs_bucket, get_gcs_storage_client
from .progress import LiveTicker

DEFAULT_CHIRP3_MODEL = "chirp_3"
DEFAULT_STT_LOCATION = "us"
CHUNK_DURATION_SEC = 1080.0  # 18 minutes (< 20-minute STT v2 word offset limit)
CHUNK_OVERLAP_SEC = 5.0

# 1-to-1 Simplified-to-Traditional character map for CJK alignment consistency in zh-TW
_SIMP_SRC = (
    "万与丑专业丛东丝丢两严丧个丰临为丽举么义乌乐乔习乡书买乱争于亏云亘亚产亩亲亵亸亿仅仆从仑仓仪们价众优会伛伞伟传伣伤伥伦伧伪伫体佣"
    "佥侠侣侥侦侧侨侩侪侬俣俦俨俩俪俫俭债倾偬偻偾偿傥傧储傩儿兑兖党兰关兴兹养兽冁内冈册写军农冯冲决况冻净凄凉减凑凛几凤凫凭凯击凿刍刘"
    "则刚创删别刬刭刹刽刿剀剂剐剑剥剧劝办务劢动励劲劳势勋勚匀匦匮区医华协单卖占卢卤卧卫却厂厅历厉压厌厍厐厕厘厢厣厦厨厩厮县叁参叆叇双"
    "发变叙叠叶号叹叽后吓吕吗吣吨听启吴呐呒呓呕呖呗员呙呛呜咏咙咛咝咤响哑哒哓哔哕哗哙哜哝哟唛唝唠唡唢唤啧啬啭啮啰啴啸喂喷喽喾嗫嗳嘘嘤"
    "嘱噜嚣团园囱围囵国图圆圣圹场坂坏块坚坛坜坝坞坟坠垄垅垆垒垦垩垫垭垯垱垲垴埘埙埚埯堑堕塆墙壮声壳壶壸处备复够头夸夹夺奁奂奋奖奥妆妇"
    "妈妩妪妫姗姹娄娅娆娇娈娱娲娴婳婴婵婶媪媭嫒嫔嫱嬷孙学孪宁宝实宠审宪宫宽宾寝对寻导寿将尔尘尝尧尴尸尽层屃屉届属屡屦屿岁岂岖岗岘岙岚"
    "岛岭岽岿峃峄峡峣峤峥峦崂崃崄崭嵘嵚嵝巅巩巯币帅师帏帐帘帜带帧帮帱帻帼幂干并广庄庆庐庑库应庙庞废庼廪开异弃弑张弥弪弯弹强归当录彟彦"
    "彷彻征径徕忆忏忧忾怀态怂怃怄怅怆怜总怼怿恋恒恳恶恸恹恺恻恼恽悦悫悬悭悮悯惊惧惨惩惫惬惭惮惯愠愤愦愿慑慭懑懒懔戆戋戏戗战戬戯户扑执"
    "扩扪扫扬扰抚抛抟抠抡抢护报担拟拢拣拥拦拧拨择挂挚挛挜挝挞挟挠挡挢挣挤挥挦挽捝捞损捡换捣据掳掴掷掸掺掼揽揾揿搀搁搂搅携摄摅摆摇摈摊"
    "撄撑撵撷撸撺擞攒敌敛敩数斋斓斗斩断无旧时旷旸昙昵昼昽显晋晒晓晔晕晖暂暧术朴机杀杂权杆杠条来杨杩杰极构枞枢枣枥枧枨枪枫枭柜柠柽栀栅"
    "标栈栉栊栋栌栎栏树栖样栾桠桡桢档桤桥桦桧桨桩梦梼梾梿检棁棂棱椁椟椠椤椭楼榄榅榇榈榉槚槛槟槠横樯樱橥橱橹橼檩欢欤欧歼殁殇残殒殓殚殡"
    "殴毁毂毕毙毡毵氇气氢氩氲汇汉汤汹沉沟没沣沤沥沦沧沨沩沪泄泞泪泶泷泸泺泻泼泽泾洁洒洼浃浅浆浇浈浉浊测浍济浏浐浑浒浓浔浕涂涌涛涝涞涟"
    "涠涡涢涣涤润涧涨涩淀渊渌渍渎渐渑渔渖渗温湾湿溁溃溅溆溇滗滚滞滟滠满滢滤滥滦滨滩滪漓漤潆潇潋潍潜潴澛澜濑濒灏灭灯灵灾灿炀炉炖炜炝点"
    "炼炽烁烂烃烛烟烦烧烨烩烫烬热焕焖焘煴爱爷牍牦牵牺犊状犷犸犹狈狝狞独狭狮狯狰狱狲猃猎猕猡猪猫猬献獭玑玙玚玛玮环现玱玺珐珑珰珲琎琏琐"
    "琼瑶瑷璎瓒瓮瓯电画畅畴疖疗疟疠疡疬疭疮疯疱疴痈痉痒痖痨痪痫瘅瘆瘗瘘瘪瘫瘾瘿癞癣癫皑皱皲盏盐监盖盗盘眍眦眬着睁睐睑睾瞆瞒瞩矫矶矾矿"
    "砀码砖砗砚砜砺砻砾础硁硕硖硗硙硚确硷碍碛碜碱礴礼祃祎祢祯祷祸禀禄禅离秃秆种积称秽秾稆税稣稳穑穷窃窍窎窑窜窝窥窦窭竖竞笃笋笔笕笺笼"
    "笾筑筚筛筜筝筹筼签简箓箦箧箨箩箪箫篑篓篮篯篱簖籁籴类籼粜粝粤粪粮糁糇紧絷纟纠纡红纣纤纥约级纨纩纪纫纬纭纮纯纰纱纲纳纴纵纶纷纸纹纺"
    "纻纼纽纾线绀绁绂练组绅细织终绉绊绋绌绍绎经绐绑绒结绔绕绖绗绘给绚绛络绝绞统绠绡绢绣绤绥绦继绨绩绪绫绬续绮绯绰绱绲绳维绵绶绷绸绹绺"
    "绻综绽绾绿缀缁缂缃缄缅缆缇缈缉缊缋缌缍缎缏缐缑缒缓缔缕编缗缘缙缚缛缜缝缞缟缠缡缢缣缤缥缦缧缨缩缪缫缬缭缮缯缰缱缲缳缴缵罂网罗罚罢"
    "罴羁羟羡翘翙翚耢耧耸耻聂聋职聍联聩聪肃肠肤肮肾肿胀胁胆胜胧胨胪胫胶脉脍脏脐脑脓脔脚脱脶脸腊腌腘腭腻腽腾膑膻臜舆舍舣舰舱舻艰艳艺节"
    "芈芗芜芦苁苇苈苋苌苍苎苏苧苹范茎茏茑茔茕茧荆荐荙荚荛荜荞荟荠荡荣荤荥荦荧荨荩荪荫荬荭荮药莅莱莲莳莴莶获莸莹莺莼萚萝萤营萦萧萨葱蒇"
    "蒉蒋蒌蓝蓟蓠蓣蓥蓦蔂蔷蔹蔺蔼蕰蕲蕴薮藓蘖虏虑虚虫虬虮虱虽虾虿蚀蚁蚂蚕蚝蚬蛊蛎蛏蛮蛰蛱蛲蛳蛴蜕蜗蜡蝇蝈蝉蝎蝼蝾螀螨蟏衅衔补衬衮袄袅"
    "袆袜袭袯装裆裈裢裣裤裥褛褴襕见观觃规觅视觇览觉觊觋觌觍觎觏觐觑觞触觯訚詟誉誊讠计订讣认讥讦讧讨让讪讫讬训议讯记讱讲讳讴讵讶讷许讹"
    "论讻讼讽设访诀证诂诃评诅识诇诈诉诊诋诌词诎诏诐译诒诓诔试诖诗诘诙诚诛诜话诞诟诠诡询诣诤该详诧诨诩诪诫诬语诮误诰诱诲诳说诵诶请诸诹"
    "诺读诼诽课诿谀谁谂调谄谅谆谇谈谊谋谌谍谎谏谐谑谒谓谔谕谖谗谘谙谚谛谜谝谞谟谠谡谢谣谤谥谦谧谨谩谪谫谬谭谮谯谰谱谲谳谴谵谶豮贝贞负"
    "贠贡财责贤败账货质贩贪贫贬购贮贯贰贱贲贳贴贵贶贷贸费贺贻贼贽贾贿赀赁赂赃资赅赆赇赈赉赊赋赌赍赎赏赐赑赒赓赔赕赖赗赘赙赚赛赜赝赞赟"
    "赠赡赢赣赪赵赶趋趱趸跃跄跞践跶跷跸跹跻踊踌踪踬踯蹑蹒蹰蹿躏躜躯车轧轨轩轪轫转轭轮软轰轱轲轳轴轵轶轷轸轹轺轻轼载轾轿辀辁辂较辄辅辆"
    "辇辈辉辊辋辌辍辎辏辐辑辒输辔辕辖辗辘辙辚辞辩辫边辽达迁过迈运还这进远违连迟迩迳迹适选逊递逦逻遗遥邓邝邬邮邹邺邻郏郐郑郓郦郧郸酂酝"
    "酦酱酽酾酿采释鉴銮錾钅钆钇针钉钊钋钌钍钎钏钐钑钒钓钔钕钖钗钘钙钚钛钜钝钞钟钠钡钢钣钤钥钦钧钨钩钪钫钬钭钮钯钰钱钲钳钴钵钶钷钸钹钺"
    "钻钼钽钾钿铀铁铂铃铄铅铆铇铈铉铊铋铌铍铎铏铐铑铒铓铔铕铖铗铘铙铚铛铜铝铞铟铠铡铢铣铤铥铦铧铨铩铪铫铬铭铮铯铰铱铲铳铴铵银铷铸铹铺"
    "铻铼铽链铿销锁锂锃锄锅锆锇锈锉锊锋锌锍锎锏锐锑锒锓锔锕锖锗锘错锚锛锜锝锞锟锠锡锢锣锤锥锦锧锨锩锪锫锬锭键锯锰锱锲锳锴锵锶锷锸锹锺"
    "锻锼锽锾锿镀镁镂镃镄镅镆镇镈镉镊镋镌镍镎镏镐镑镒镓镔镕镖镗镘镙镚镛镜镝镞镟镠镡镢镣镤镥镦镧镨镩镪镫镬镭镮镯镰镱镲镳镴镵镶长门闩闪"
    "闫闬闭问闯闰闱闲闳间闵闶闷闸闹闺闻闼闽闾闿阀阁阂阃阄阅阆阇阈阉阊阋阌阍阎阏阐阑阒阓阔阕阖阗阘阙阚阛队阳阴阵阶际陆陇陈陉陕陧陨险随"
    "隐隶隽难雏雠雳雾霁霡霭靓静靥鞑鞒鞯韦韧韨韩韪韫韬韵页顶顷顸项顺须顼顽顾顿颀颁颂颃预颅领颇颈颉颊颋颌颍颎颏颐频颒颓颔颕颖颗题颙颚颛"
    "颜额颞颟颠颡颢颤颥颦颧风飏飐飑飒飓飔飕飖飗飘飙飚飞飨餍饣饤饥饦饧饨饩饪饫饬饭饮饯饰饱饲饳饴饵饶饷饸饹饺饻饼饽饾饿馀馁馂馃馄馅馆馇"
    "馈馉馊馋馌馍馎馏馐馑馒馓馔馕马驭驮驯驰驱驲驳驴驵驶驷驸驹驺驻驼驽驾驿骀骁骂骃骄骅骆骇骈骉骊骋验骍骎骏骐骑骒骓骔骕骖骗骘骙骚骛骜骝"
    "骞骟骠骡骢骣骤骥骦骧髅髋髌鬓鬶魇魉鱼鱽鱾鱿鲀鲁鲂鲃鲄鲅鲆鲇鲈鲉鲊鲋鲌鲍鲎鲏鲐鲑鲒鲓鲔鲕鲖鲗鲘鲙鲚鲛鲜鲝鲞鲟鲠鲡鲢鲣鲤鲥鲦鲧鲨鲩鲪"
    "鲫鲬鲭鲮鲯鲰鲱鲲鲳鲴鲵鲶鲷鲸鲹鲺鲻鲼鲽鲾鲿鳀鳁鳂鳃鳄鳅鳆鳇鳈鳉鳊鳋鳌鳍鳎鳏鳐鳑鳒鳓鳔鳕鳖鳗鳘鳙鳚鳛鳜鳝鳞鳟鳠鳡鳢鳣鳤鸟鸠鸡鸢鸣鸤"
    "鸥鸦鸧鸨鸩鸪鸫鸬鸭鸮鸯鸰鸱鸲鸳鸴鸵鸶鸷鸸鸹鸺鸻鸼鸽鸾鸿鹀鹁鹂鹃鹄鹅鹆鹇鹈鹉鹊鹋鹌鹍鹎鹏鹐鹑鹒鹓鹔鹕鹖鹗鹘鹙鹚鹛鹜鹝鹞鹟鹠鹡鹢鹣鹤"
    "鹥鹦鹧鹨鹩鹪鹫鹬鹭鹮鹯鹰鹱鹲鹳鹴鹾麦麸麹黄黉黡黩黪黾鼋鼍鼗鼹齐齑齿龀龁龂龃龄龅龆龇龈龉龊龋龌龙龚龛龟鿎鿏鿔鿭"
)
_TRAD_DST = (
    "萬與醜專業叢東絲丟兩嚴喪個豐臨為麗舉麼義烏樂喬習鄉書買亂爭於虧雲亙亞產畝親褻嚲億僅僕從侖倉儀們價眾優會傴傘偉傳俔傷倀倫傖偽佇體傭"
    "僉俠侶僥偵側僑儈儕儂俁儔儼倆儷倈儉債傾傯僂僨償儻儐儲儺兒兌兗黨蘭關興茲養獸囅內岡冊寫軍農馮衝決況凍淨淒涼減湊凜幾鳳鳧憑凱擊鑿芻劉"
    "則剛創刪別剗剄剎劊劌剴劑剮劍剝劇勸辦務勱動勵勁勞勢勳勩勻匭匱區醫華協單賣佔盧鹵臥衛卻廠廳歷厲壓厭厙龎廁釐廂厴廈廚廄廝縣叄參靉靆雙"
    "發變敘疊葉號嘆嘰後嚇呂嗎唚噸聽啓吳吶嘸囈嘔嚦唄員咼嗆嗚詠嚨嚀噝吒響啞噠嘵嗶噦嘩噲嚌噥喲嘜嗊嘮啢嗩喚嘖嗇囀嚙囉嘽嘯餵噴嘍嚳囁噯噓嚶"
    "囑嚕囂團園囪圍圇國圖圓聖壙場阪壞塊堅壇壢壩塢墳墜壟壠壚壘墾堊墊埡墶壋塏堖塒塤堝垵塹墮壪牆壯聲殼壺壼處備復夠頭誇夾奪奩奐奮獎奧妝婦"
    "媽嫵嫗媯姍奼婁婭嬈嬌孌娛媧嫻嫿嬰嬋嬸媼嬃嬡嬪嬙嬤孫學孿寧寶實寵審憲宮寬賓寢對尋導壽將爾塵嘗堯尷屍盡層屓屜屆屬屢屨嶼歲豈嶇崗峴嶴嵐"
    "島嶺崬巋嶨嶧峽嶢嶠崢巒嶗崍嶮嶄嶸嶔嶁巔鞏巰幣帥師幃帳簾幟帶幀幫幬幘幗冪乾並廣莊慶廬廡庫應廟龐廢廎廩開異棄弒張彌弳彎彈強歸當錄彠彥"
    "徬徹徵徑徠憶懺憂愾懷態慫憮慪悵愴憐總懟懌戀恆懇惡慟懨愷惻惱惲悅愨懸慳悞憫驚懼慘懲憊愜慚憚慣慍憤憒願懾憖懣懶懍戇戔戲戧戰戩戱戶撲執"
    "擴捫掃揚擾撫拋摶摳掄搶護報擔擬攏揀擁攔擰撥擇掛摯攣掗撾撻挾撓擋撟掙擠揮撏輓挩撈損撿換搗據擄摑擲撣摻摜攬搵撳攙擱摟攪攜攝攄擺搖擯攤"
    "攖撐攆擷擼攛擻攢敵斂斆數齋斕鬥斬斷無舊時曠暘曇暱晝曨顯晉曬曉曄暈暉暫曖術樸機殺雜權桿槓條來楊榪傑極構樅樞棗櫪梘棖槍楓梟櫃檸檉梔柵"
    "標棧櫛櫳棟櫨櫟欄樹棲樣欒椏橈楨檔榿橋樺檜槳樁夢檮棶槤檢梲櫺稜槨櫝槧欏橢樓欖榲櫬櫚櫸檟檻檳櫧橫檣櫻櫫櫥櫓櫞檁歡歟歐殲歿殤殘殞殮殫殯"
    "毆毀轂畢斃氈毿氌氣氫氬氳匯漢湯洶沈溝沒灃漚瀝淪滄渢溈滬洩濘淚澩瀧瀘濼瀉潑澤涇潔灑窪浹淺漿澆湞溮濁測澮濟瀏滻渾滸濃潯濜塗湧濤澇淶漣"
    "潿渦溳渙滌潤澗漲澀澱淵淥漬瀆漸澠漁瀋滲溫灣濕濚潰濺漵漊潷滾滯灧灄滿瀅濾濫灤濱灘澦灕灠瀠瀟瀲濰潛瀦瀂瀾瀨瀕灝滅燈靈災燦煬爐燉煒熗點"
    "煉熾爍爛烴燭煙煩燒燁燴燙燼熱煥燜燾熅愛爺牘氂牽犧犢狀獷獁猶狽獮獰獨狹獅獪猙獄猻獫獵獼玀豬貓蝟獻獺璣璵瑒瑪瑋環現瑲璽琺瓏璫琿璡璉瑣"
    "瓊瑤璦瓔瓚甕甌電畫暢疇癤療瘧癘瘍癧瘲瘡瘋皰痾癰痙癢瘂癆瘓癇癉瘮瘞瘻癟癱癮癭癩癬癲皚皺皸盞鹽監蓋盜盤瞘眥矓著睜睞瞼睪瞶瞞矚矯磯礬礦"
    "碭碼磚硨硯碸礪礱礫礎硜碩硤磽磑礄確礆礙磧磣鹼礡禮禡禕禰禎禱禍稟祿禪離禿稈種積稱穢穠穭稅穌穩穡窮竊竅窵窯竄窩窺竇窶竪競篤筍筆筧箋籠"
    "籩築篳篩簹箏籌篔簽簡籙簀篋籜籮簞簫簣簍籃籛籬籪籟糴類秈糶糲粵糞糧糝餱緊縶糹糾紆紅紂纖紇約級紈纊紀紉緯紜紘純紕紗綱納紝縱綸紛紙紋紡"
    "紵紖紐紓線紺紲紱練組紳細織終縐絆紼絀紹繹經紿綁絨結絝繞絰絎繪給絢絳絡絕絞統綆綃絹繡綌綏縧繼綈績緒綾緓續綺緋綽緔緄繩維綿綬繃綢綯綹"
    "綣綜綻綰綠綴緇緙緗緘緬纜緹緲緝縕繢緦綞緞緶線緱縋緩締縷編緡緣縉縛縟縝縫縗縞纏縭縊縑繽縹縵縲纓縮繆繅纈繚繕繒繮繾繰繯繳纘罌網羅罰罷"
    "羆羈羥羨翹翽翬耮耬聳恥聶聾職聹聯聵聰肅腸膚骯腎腫脹脅膽勝朧腖臚脛膠脈膾髒臍腦膿臠腳脫腡臉臘醃膕齶膩膃騰臏羶臢輿捨艤艦艙艫艱艷藝節"
    "羋薌蕪蘆蓯葦藶莧萇蒼苧蘇薴蘋範莖蘢蔦塋煢繭荊薦薘莢蕘蓽蕎薈薺蕩榮葷滎犖熒蕁藎蓀蔭蕒葒葤藥蒞萊蓮蒔萵薟獲蕕瑩鶯蒓蘀蘿螢營縈蕭薩蔥蕆"
    "蕢蔣蔞藍薊蘺蕷鎣驀虆薔蘞藺藹薀蘄蘊藪蘚櫱虜慮虛蟲虯蟣蝨雖蝦蠆蝕蟻螞蠶蠔蜆蠱蠣蟶蠻蟄蛺蟯螄蠐蛻蝸蠟蠅蟈蟬蠍螻蠑螿蟎蠨釁銜補襯袞襖裊"
    "褘襪襲襏裝襠褌褳襝褲襇褸襤襴見觀覎規覓視覘覽覺覬覡覿覥覦覯覲覷觴觸觶誾讋譽謄訁計訂訃認譏訐訌討讓訕訖託訓議訊記訒講諱謳詎訝訥許訛"
    "論訩訟諷設訪訣證詁訶評詛識詗詐訴診詆謅詞詘詔詖譯詒誆誄試詿詩詰詼誠誅詵話誕詬詮詭詢詣諍該詳詫諢詡譸誡誣語誚誤誥誘誨誑說誦誒請諸諏"
    "諾讀諑誹課諉諛誰諗調諂諒諄誶談誼謀諶諜謊諫諧謔謁謂諤諭諼讒諮諳諺諦謎諞諝謨讜謖謝謠謗謚謙謐謹謾謫謭謬譚譖譙讕譜譎讞譴譫讖豶貝貞負"
    "貟貢財責賢敗賬貨質販貪貧貶購貯貫貳賤賁貰貼貴貺貸貿費賀貽賊贄賈賄貲賃賂贓資賅贐賕賑賚賒賦賭賫贖賞賜贔賙賡賠賧賴賵贅賻賺賽賾贋贊贇"
    "贈贍贏贛赬趙趕趨趲躉躍蹌躒踐躂蹺蹕躚躋踴躊蹤躓躑躡蹣躕躥躪躦軀車軋軌軒軑軔轉軛輪軟轟軲軻轤軸軹軼軤軫轢軺輕軾載輊轎輈輇輅較輒輔輛"
    "輦輩輝輥輞輬輟輜輳輻輯轀輸轡轅轄輾轆轍轔辭辯辮邊遼達遷過邁運還這進遠違連遲邇逕跡適選遜遞邐邏遺遙鄧鄺鄔郵鄒鄴鄰郟鄶鄭鄆酈鄖鄲酇醖"
    "醱醬釅釃釀採釋鑒鑾鏨釒釓釔針釘釗釙釕釷釺釧釤鈒釩釣鍆釹鍚釵鈃鈣鈈鈦鉅鈍鈔鐘鈉鋇鋼鈑鈐鑰欽鈞鎢鈎鈧鈁鈥鈄鈕鈀鈺錢鉦鉗鈷鉢鈳鉕鈽鈸鉞"
    "鑽鉬鉭鉀鈿鈾鐵鉑鈴鑠鉛鉚鉋鈰鉉鉈鉍鈮鈹鐸鉶銬銠鉺鋩錏銪鋮鋏鋣鐃銍鐺銅鋁銱銦鎧鍘銖銑鋌銩銛鏵銓鎩鉿銚鉻銘錚銫鉸銥鏟銃鐋銨銀銣鑄鐒鋪"
    "鋙錸鋱鏈鏗銷鎖鋰鋥鋤鍋鋯鋨鏽銼鋝鋒鋅鋶鐦鐧銳銻鋃鋟鋦錒錆鍺鍩錯錨錛錡鍀錁錕錩錫錮鑼錘錐錦鑕鍁錈鍃錇錟錠鍵鋸錳錙鍥鍈鍇鏘鍶鍔鍤鍬鍾"
    "鍛鎪鍠鍰鎄鍍鎂鏤鎡鐨鎇鏌鎮鎛鎘鑷鎲鐫鎳鎿鎦鎬鎊鎰鎵鑌鎔鏢鏜鏝鏍鏰鏞鏡鏑鏃鏇鏐鐔鐝鐐鏷鑥鐓鑭鐠鑹鏹鐙鑊鐳鐶鐲鐮鐿鑔鑣鑞鑱鑲長門閂閃"
    "閆閈閉問闖閏闈閒閎間閔閌悶閘鬧閨聞闥閩閭闓閥閣閡閫鬮閱閬闍閾閹閶鬩閿閽閻閼闡闌闃闠闊闋闔闐闒闕闞闤隊陽陰陣階際陸隴陳陘陝隉隕險隨"
    "隱隸雋難雛讎靂霧霽霢靄靚靜靨韃鞽韉韋韌韍韓韙韞韜韻頁頂頃頇項順須頊頑顧頓頎頒頌頏預顱領頗頸頡頰頲頜潁熲頦頤頻頮頹頷頴穎顆題顒顎顓"
    "顏額顳顢顛顙顥顫顬顰顴風颺颭颮颯颶颸颼颻飀飄飆飈飛饗饜飠飣飢飥餳飩餼飪飫飭飯飲餞飾飽飼飿飴餌饒餉餄餎餃餏餅餑餖餓餘餒餕餜餛餡館餷"
    "饋餶餿饞饁饃餺餾饈饉饅饊饌饢馬馭馱馴馳驅馹駁驢駔駛駟駙駒騶駐駝駑駕驛駘驍罵駰驕驊駱駭駢驫驪騁驗騂駸駿騏騎騍騅騌驌驂騙騭騤騷騖驁騮"
    "騫騸驃騾驄驏驟驥驦驤髏髖髕鬢鬹魘魎魚魛魢魷魨魯魴䰾魺鮁鮃鮎鱸鮋鮓鮒鮊鮑鱟鮍鮐鮭鮚鮳鮪鮞鮦鰂鮜鱠鱭鮫鮮鮺鮝鱘鯁鱺鰱鰹鯉鰣鰷鯀鯊鯇鮶"
    "鯽鯒鯖鯪鯕鯫鯡鯤鯧鯝鯢鯰鯛鯨鰺鯴鯔鱝鰈鰏鱨鯷鰮鰃鰓鰐鰍鰒鰉鰁鱂鯿鰠鰲鰭鰨鰥鰩鰟鰜鰳鰾鱈鱉鰻鰵鱅䲁鰼鱖鱔鱗鱒鱯鱤鱧鱣䲘鳥鳩雞鳶鳴鳲"
    "鷗鴉鶬鴇鴆鴣鶇鸕鴨鴞鴦鴒鴟鴝鴛鷽鴕鷥鷙鴯鴰鵂鴴鵃鴿鸞鴻鵐鵓鸝鵑鵠鵝鵒鷳鵜鵡鵲鶓鵪鵾鵯鵬鵮鶉鶊鵷鷫鶘鶡鶚鶻鶖鷀鶥鶩鷊鷂鶲鶹鶺鷁鶼鶴"
    "鷖鸚鷓鷚鷯鷦鷲鷸鷺䴉鸇鷹鸌鸏鸛鸘鹺麥麩麴黃黌黶黷黲黽黿鼉鞀鼴齊齏齒齔齕齗齟齡齙齠齜齦齬齪齲齷龍龔龕龜䃮䥑鎶鉨"
)
_SIMP_TO_TRAD_MAP = str.maketrans(_SIMP_SRC, _TRAD_DST)


def _is_cjk_char(ch):
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
    )


def _parse_offset_seconds(offset_val):
    if offset_val is None:
        return 0.0
    if isinstance(offset_val, (int, float)):
        return float(offset_val)
    s = str(offset_val).strip().rstrip("s")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _get_audio_duration(audio_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return float(res.stdout.strip())


def resolve_chirp3_language_codes(language="auto", initial_prompt=None):
    """
    Resolve Cloud STT v2 BCP-47 language codes and effective locale tag.
    Supports code-switching by pairing non-English primary locales with 'en-US'.
    Returns: (language_codes_list, fallback_detected_locale)
    """
    raw = str(language or "auto").strip()
    lower = raw.lower().replace("_", "-")

    if lower in ("auto", "none", ""):
        if initial_prompt:
            if re.search(r"[\u3040-\u30ff]", initial_prompt):
                return ["ja-JP", "en-US"], "ja"
            if re.search(r"[\uac00-\ud7af]", initial_prompt):
                return ["ko-KR", "en-US"], "ko"
            if "简体中文" in initial_prompt or "专有名词" in initial_prompt:
                return ["cmn-Hans-CN", "en-US"], "zh-CN"
            if re.search(r"[\u4e00-\u9fff]", initial_prompt):
                return ["cmn-Hant-TW", "en-US"], "zh-TW"
        return ["auto"], "zh-TW"

    lang_map = {
        "zh": ("cmn-Hant-TW", "zh-TW"),
        "zh-tw": ("cmn-Hant-TW", "zh-TW"),
        "zh-hant": ("cmn-Hant-TW", "zh-TW"),
        "cmn-hant-tw": ("cmn-Hant-TW", "zh-TW"),
        "zh-cn": ("cmn-Hans-CN", "zh-CN"),
        "zh-hans": ("cmn-Hans-CN", "zh-CN"),
        "cmn-hans-cn": ("cmn-Hans-CN", "zh-CN"),
        "ja": ("ja-JP", "ja"),
        "ja-jp": ("ja-JP", "ja"),
        "ko": ("ko-KR", "ko"),
        "ko-kr": ("ko-KR", "ko"),
        "en": ("en-US", "en"),
        "en-us": ("en-US", "en"),
        "en-gb": ("en-GB", "en"),
    }
    primary_bcp47, norm_loc = lang_map.get(lower, ("en-US", "en"))
    if not primary_bcp47.lower().startswith("en-"):
        return [primary_bcp47, "en-US"], norm_loc
    return [primary_bcp47], norm_loc


def _stt_rest_request(method, url, credentials, payload=None, max_retries=4, initial_delay=2.0, op_name="Cloud STT v2"):
    """Send an authenticated REST request to Cloud Speech-to-Text v2 with transient error retry."""
    last_err = None
    for attempt in range(max_retries):
        try:
            if not credentials.valid:
                credentials.refresh(GoogleAuthRequest())
            headers = {
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json; charset=utf-8",
            }
            data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw_body = resp.read().decode("utf-8")
                return json.loads(raw_body) if raw_body.strip() else {}
        except urllib.error.HTTPError as e:
            status_code = e.code
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if status_code in (401, 403):
                raise RuntimeError(
                    f"{op_name} failed with HTTP {status_code} (Authentication/Permission Denied): {err_body}"
                ) from e
            if status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                last_err = e
                sleep_sec = initial_delay * (2.0 ** attempt) * (1.0 + random.uniform(-0.15, 0.15))
                time.sleep(max(0.5, sleep_sec))
                continue
            raise RuntimeError(f"{op_name} failed with HTTP {status_code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(max(0.5, initial_delay * (2.0 ** attempt)))
                continue
            raise
    if last_err:
        raise last_err
    return {}


def _poll_stt_operation(base_url, operation_name, credentials, poll_interval_sec=2.0, timeout_sec=900.0):
    """Poll a Cloud Speech-to-Text v2 Long-Running Operation until completion."""
    op_url = f"{base_url}/{operation_name.lstrip('/')}"
    t_start = time.time()
    while True:
        op_data = _stt_rest_request("GET", op_url, credentials, op_name="Poll Chirp 3 BatchRecognize")
        if op_data.get("done"):
            if "error" in op_data:
                err_obj = op_data["error"]
                raise RuntimeError(
                    f"Chirp 3 BatchRecognize failed: [{err_obj.get('code')}] {err_obj.get('message')}"
                )
            return op_data
        if time.time() - t_start > timeout_sec:
            raise TimeoutError(f"Chirp 3 BatchRecognize timed out after {timeout_sec:.0f}s.")
        time.sleep(poll_interval_sec)


def _compute_chunk_windows(total_duration):
    """Split audio > 18 minutes into 18-minute windows with 5-second overlap."""
    if total_duration <= CHUNK_DURATION_SEC:
        return [(0.0, total_duration)]
    windows = []
    idx = 0
    while True:
        start_sec = 0.0 if idx == 0 else max(0.0, idx * CHUNK_DURATION_SEC - CHUNK_OVERLAP_SEC)
        end_sec = min(total_duration, (idx + 1) * CHUNK_DURATION_SEC)
        if end_sec - start_sec < 1.0 and windows:
            prev_s, _ = windows[-1]
            if total_duration - prev_s <= 1180.0:
                windows[-1] = (prev_s, total_duration)
            break
        windows.append((round(start_sec, 3), round(end_sec, 3)))
        if end_sec >= total_duration:
            break
        idx += 1
    return windows


def _suppress_repetition_loops(words, max_repeats=3, max_ngram_len=8):
    """Suppress generative decoding repetition loops in music or noise intervals."""
    if len(words) < 4:
        return list(words)
    norm_tokens = [
        "".join(
            ch.lower() for ch in str(w.get("word", ""))
            if not unicodedata.category(ch).startswith(("P", "S"))
        ).strip()
        for w in words
    ]
    n_total = len(words)
    keep_indices = []
    i = 0
    while i < n_total:
        loop_found = False
        for ngram_size in range(min(max_ngram_len, (n_total - i) // 2), 0, -1):
            pattern = norm_tokens[i : i + ngram_size]
            if not any(pattern):
                continue
            repeat_count = 1
            pos = i + ngram_size
            while pos + ngram_size <= n_total and norm_tokens[pos : pos + ngram_size] == pattern:
                repeat_count += 1
                pos += ngram_size
            threshold = 3 if ngram_size >= 3 else (max_repeats + 1)
            if repeat_count >= threshold:
                for k in range(i, i + ngram_size):
                    keep_indices.append(k)
                i = pos
                loop_found = True
                break
        if not loop_found:
            keep_indices.append(i)
            i += 1
    return [words[idx] for idx in keep_indices]


def _join_words_for_display(word_strs):
    """Join word tokens without inserting artificial spaces between CJK characters."""
    if not word_strs:
        return ""
    out = []
    for token in word_strs:
        tok = str(token).strip()
        if not tok:
            continue
        if not out:
            out.append(tok)
            continue
        prev_last = out[-1][-1]
        curr_first = tok[0]
        if (
            (_is_cjk_char(prev_last) and _is_cjk_char(curr_first))
            or (_is_cjk_char(prev_last) and curr_first in "，。！？；：、）」』】》")
            or (prev_last in "，。！？；：、（）「」『』【】《》" and _is_cjk_char(curr_first))
            or (not _is_cjk_char(prev_last) and curr_first in ",.!?;:)]}%")
        ):
            out.append(tok)
        else:
            out.append(" " + tok)
    return "".join(out).strip()


def _group_words_into_segments(all_words, norm_lang="zh-TW"):
    """Group timed words into natural Whisper-sized acoustic segments for Stage 3 proofreading."""
    if not all_words:
        return []

    is_cjk = norm_lang in ("zh-TW", "zh-CN", "ja", "ko")
    soft_w = 15.0 if is_cjk else 38.0
    hard_w = 24.0 if is_cjk else 60.0

    segments = []
    cur_words = []
    cur_width = 0.0

    for idx, w in enumerate(all_words):
        w_str = str(w["word"]).strip()
        if not w_str:
            continue
        cur_words.append(w)
        w_disp = sum(1.0 if ord(c) > 127 else 0.5 for c in w_str) if is_cjk else (len(w_str) + 1.0)
        cur_width += w_disp

        should_break = False
        if idx + 1 < len(all_words):
            gap = float(all_words[idx + 1]["start"]) - float(w["end"])
            seg_dur = float(w["end"]) - float(cur_words[0]["start"])
            ends_sent = bool(re.search(r"[。！？.!?][\"'”’）\)]*$", w_str))
            ends_clause = bool(re.search(r"[，、；：,;:\-][\"'”’）\)]*$", w_str))

            if gap >= 0.35 or ends_sent:
                should_break = True
            elif cur_width >= soft_w and (gap >= 0.10 or ends_clause):
                should_break = True
            elif cur_width >= hard_w or seg_dur >= 5.5:
                should_break = True
        else:
            should_break = True

        if should_break and cur_words:
            seg_text = _join_words_for_display([cw["word"] for cw in cur_words])
            segments.append({
                "index": len(segments) + 1,
                "start": round(float(cur_words[0]["start"]), 3),
                "end": round(float(cur_words[-1]["end"]), 3),
                "text": seg_text,
                "words": list(cur_words),
            })
            cur_words = []
            cur_width = 0.0

    return segments


def run_chirp3_transcription(
    audio_wav,
    language="auto",
    initial_prompt=None,
    project=None,
    gcs_bucket=None,
    region="us-central1",
    stt_location=None,
    model_name=DEFAULT_CHIRP3_MODEL,
):
    """
    Run Stage 2 acoustic word-timestamp transcription using Google Cloud Speech-to-Text V2 (chirp_3).
    Returns: (sub_list, detected_lang, all_words) matching run_whisper_transcription's exact schema.
    """
    resolved_stt_loc = (stt_location or os.environ.get("STT_LOCATION") or DEFAULT_STT_LOCATION).strip().lower()
    lang_codes, detected_lang = resolve_chirp3_language_codes(language=language, initial_prompt=initial_prompt)

    print(
        f"\n[Stage 2/3] Running Google Cloud STT V2 Chirp 3 transcription "
        f"(Model: {model_name}, STT Location: {resolved_stt_loc}, Langs: {', '.join(lang_codes)}, Word Offsets: ON)..."
    )
    t0 = time.time()

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    credentials, adc_project = google.auth.default(scopes=scopes)
    if not credentials.valid:
        credentials.refresh(GoogleAuthRequest())

    resolved_project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or adc_project
    if not resolved_project or not gcs_bucket:
        raise RuntimeError("Chirp 3 requires both Google Cloud Project ID and GCS Bucket.")

    bucket_clean = gcs_bucket.replace("gs://", "").strip("/")
    ensure_gcs_bucket(bucket_clean, project=resolved_project, region=region)
    storage_client = get_gcs_storage_client(project=resolved_project)
    bucket_obj = storage_client.bucket(bucket_clean)

    base_url = "https://speech.googleapis.com/v2" if resolved_stt_loc == "global" else f"https://{resolved_stt_loc}-speech.googleapis.com/v2"
    recognize_url = f"{base_url}/projects/{resolved_project}/locations/{resolved_stt_loc}/recognizers/_:batchRecognize"

    run_id = uuid.uuid4().hex[:10]
    run_gcs_prefix = f"raw/chirp3_asr_{run_id}"
    temp_mp3 = os.path.join(tempfile.gettempdir(), f"chirp3_sub_{run_id}.mp3")

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_wav), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", temp_mp3],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        total_duration = _get_audio_duration(temp_mp3)
        windows = _compute_chunk_windows(total_duration)
        num_chunks = len(windows)

        def _process_chunk(chunk_idx):
            c_start, c_end = windows[chunk_idx]
            c_dur = c_end - c_start
            chunk_local = temp_mp3
            is_slice = False
            if num_chunks > 1 or c_start > 0.0:
                chunk_local = os.path.join(tempfile.gettempdir(), f"chirp3_sub_{run_id}_c{chunk_idx:02d}.mp3")
                is_slice = True
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-ss", str(c_start), "-t", str(c_dur),
                        "-i", temp_mp3, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", chunk_local,
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                )

            try:
                blob_name = f"{run_gcs_prefix}/chunks/chunk_{chunk_idx:02d}.mp3"
                out_prefix = f"gs://{bucket_clean}/{run_gcs_prefix}/results/chunk_{chunk_idx:02d}/"
                blob = bucket_obj.blob(blob_name)
                blob.upload_from_filename(chunk_local, content_type="audio/mpeg")
                chunk_gcs_uri = f"gs://{bucket_clean}/{blob_name}"

                payload = {
                    "config": {
                        "model": model_name,
                        "languageCodes": lang_codes,
                        "features": {
                            "enableWordTimeOffsets": True,
                        },
                        "autoDecodingConfig": {},
                    },
                    "files": [{"uri": chunk_gcs_uri}],
                    "recognitionOutputConfig": {
                        "gcsOutputConfig": {"uri": out_prefix}
                    },
                }

                op_init = _stt_rest_request("POST", recognize_url, credentials, payload=payload, op_name=f"Chirp 3 Chunk {chunk_idx + 1}/{num_chunks}")
                op_name = op_init.get("name", "")
                if not op_name:
                    raise RuntimeError(f"Chirp 3 BatchRecognize did not return operation name: {op_init}")

                op_done = _poll_stt_operation(base_url, op_name, credentials)

                # Fetch output JSON from GCS
                payloads = []
                resp_results = op_done.get("response", {}).get("results", {})
                if isinstance(resp_results, dict):
                    for _f_uri, f_res in resp_results.items():
                        if isinstance(f_res, dict) and "error" in f_res and f_res["error"].get("code", 0) != 0:
                            err_info = f_res["error"]
                            raise RuntimeError(f"Chirp 3 file error: [{err_info.get('code')}] {err_info.get('message')}")
                        cs_uri = f_res.get("cloudStorageResult", {}).get("uri") or f_res.get("uri")
                        if cs_uri and cs_uri.startswith(f"gs://{bucket_clean}/"):
                            res_blob = bucket_obj.blob(cs_uri[len(f"gs://{bucket_clean}/"):])
                            if res_blob.exists():
                                payloads.append(json.loads(res_blob.download_as_text(encoding="utf-8")))

                if not payloads:
                    clean_p = f"{run_gcs_prefix}/results/chunk_{chunk_idx:02d}/"
                    for b_item in storage_client.list_blobs(bucket_clean, prefix=clean_p):
                        if b_item.name.endswith(".json"):
                            payloads.append(json.loads(b_item.download_as_text(encoding="utf-8")))

                chunk_words = []
                chunk_lang_code = None
                for jp in payloads:
                    for res_item in jp.get("results", []):
                        if not chunk_lang_code and res_item.get("languageCode"):
                            chunk_lang_code = res_item.get("languageCode")
                        alts = res_item.get("alternatives", [])
                        if not alts:
                            continue
                        for w in alts[0].get("words", []):
                            w_str = str(w.get("word", "")).strip()
                            if not w_str:
                                continue
                            st_local = _parse_offset_seconds(w.get("startOffset"))
                            et_local = max(st_local + 0.02, _parse_offset_seconds(w.get("endOffset")))
                            chunk_words.append({
                                "word": w_str,
                                "start": round(st_local + c_start, 3),
                                "end": round(et_local + c_start, 3),
                            })

                return chunk_idx, c_start, c_end, chunk_words, chunk_lang_code
            finally:
                if is_slice and os.path.exists(chunk_local):
                    try:
                        os.remove(chunk_local)
                    except Exception:
                        pass

        with LiveTicker(f"Running Cloud STT V2 Chirp 3 BatchRecognize ({num_chunks} chunk(s), word offsets ON)"):
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(max(1, num_chunks), 6)) as executor:
                futs = [executor.submit(_process_chunk, idx) for idx in range(num_chunks)]
                for fut in concurrent.futures.as_completed(futs):
                    results.append(fut.result())

        results.sort(key=lambda x: x[0])

        # Update detected_lang if auto-detected by STT v2
        if lang_codes == ["auto"]:
            for _, _, _, _, c_lang in results:
                if c_lang:
                    cl = c_lang.lower()
                    if cl.startswith("cmn-hans") or cl.startswith("zh-cn"):
                        detected_lang = "zh-CN"
                    elif cl.startswith("cmn") or cl.startswith("zh") or cl.startswith("yue"):
                        detected_lang = "zh-TW"
                    elif cl.startswith("ja"):
                        detected_lang = "ja"
                    elif cl.startswith("ko"):
                        detected_lang = "ko"
                    elif cl.startswith("en"):
                        detected_lang = "en"
                    break

        # Stitch chunks with overlap midpoint deduplication
        merged_words = []
        for idx, (_, c_start, c_end, c_words, _) in enumerate(results):
            min_sec = 0.0
            max_sec = float("inf")
            if idx > 0:
                _, _, prev_end, _, _ = results[idx - 1]
                min_sec = (c_start + prev_end) / 2.0 if prev_end > c_start else c_start
            if idx + 1 < num_chunks:
                _, next_start, _, _, _ = results[idx + 1]
                if c_end > next_start:
                    max_sec = (next_start + c_end) / 2.0

            for w in c_words:
                center = (w["start"] + w["end"]) / 2.0
                if idx > 0 and center < min_sec:
                    continue
                if idx + 1 < num_chunks and center >= max_sec:
                    continue
                w_text = w["word"]
                if detected_lang == "zh-TW" and _SIMP_TO_TRAD_MAP:
                    w_text = w_text.translate(_SIMP_TO_TRAD_MAP)
                merged_words.append({
                    "word": w_text,
                    "start": w["start"],
                    "end": w["end"],
                })

        all_words = _suppress_repetition_loops(merged_words)
        sub_list = _group_words_into_segments(all_words, norm_lang=detected_lang)

        elapsed = time.time() - t0
        print(
            f"  ✓ Chirp 3 acoustic transcription complete in {elapsed:.1f}s "
            f"({len(sub_list)} segments, {len(all_words)} words, language: '{detected_lang}')"
        )
        return sub_list, detected_lang, all_words

    finally:
        if os.path.exists(temp_mp3):
            try:
                os.remove(temp_mp3)
            except Exception:
                pass
        try:
            for b_del in list(storage_client.list_blobs(bucket_clean, prefix=f"{run_gcs_prefix}/")):
                try:
                    b_del.delete()
                except Exception:
                    pass
        except Exception:
            pass
