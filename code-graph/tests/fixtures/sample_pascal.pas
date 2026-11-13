unit Greeter;

interface

uses
  SysUtils, Classes, Logger;

type
  TGreeter = class(TObject)
  private
    FName: string;
  public
    constructor Create(const AName: string);
    function Greet: string;
    procedure Run;
  end;

  TGreetingKind = (gkFormal, gkCasual);

implementation

uses
  Demo;

constructor TGreeter.Create(const AName: string);
begin
  FName := AName;
  LogInfo('created');
end;

function TGreeter.Greet: string;
begin
  Result := Format('%s', [FName]);
  WriteLn(Result);
end;

procedure TGreeter.Run;
begin
  Helper;
  Greet;
end;

procedure Helper;
begin
  WriteLn('helper');
end;

end.
